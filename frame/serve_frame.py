#!/usr/bin/env python3
"""Serve the bird collage to a networked e-paper panel (XIAO ESP32 + Spectra 6).

display.py drives a Pimoroni Inky attached to the same Pi: it hands the
`inky` library an RGB image and the library does the 6-ink mapping and the
SPI protocol. A XIAO ePaper Panel is a different shape of problem - the
panel hangs off an ESP32 across the network, so the chain has to be cut
somewhere. The sensible cut is after quantization: a microcontroller
should not be running Floyd-Steinberg over 800x480.

So this serves what the ESP32 can blit straight to the panel:

    GET /state        {"signature": "...", "updated": ..., "bytes": ...}
    GET /frame.bin    packed 4-bits-per-pixel buffer (ETag = signature)
    GET /frame.png    the same frame as a PNG, for eyeballing on a laptop

The signature gate from display.py is kept here rather than on the device.
A XIAO waking on battery can then spend one small request to learn nothing
changed, instead of pulling ~187 KB every time - WiFi association is the
dominant cost in that power budget. Send If-None-Match with the last
signature and an unchanged frame answers 304.

    python3 serve_frame.py --config ~/.birdframe/config.toml --port 8080

Pixel format: two pixels per byte, high nibble first. The nibble values are
the panel's own colour indices, which differ between controllers - see
--index-map if the colours come out permuted.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from display import (  # noqa: E402  - display.py imports inky lazily, so this is safe
    DEFAULTS, SPECTRA6, _auth, fetch_species, load_config, obtain_image, signature,
)

# PIL palette order is SPECTRA6's: paper, black, red, yellow, blue, green.
# Spectra 6 controllers commonly want black=0, white=1, yellow=2, red=3,
# blue=5, green=6 (note 4 is skipped on several of them). Map PIL index ->
# panel nibble. Override with --index-map if your panel disagrees; getting
# it wrong only permutes colours, so it is easy to spot and easy to fix.
DEFAULT_INDEX_MAP = [1, 0, 3, 2, 5, 6]


def quantize_p(img: Image.Image) -> Image.Image:
    """RGB -> 'P' image whose indices are 0..5 in SPECTRA6 order."""
    pal = Image.new("P", (1, 1))
    flat = [c for ink in SPECTRA6 for c in ink]
    flat += list(SPECTRA6[0]) * ((768 - len(flat)) // 3)
    pal.putpalette(flat[:768])
    return img.convert("RGB").quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG)


def fit_and_mat(img: Image.Image, w: int, h: int, mat: float) -> Image.Image:
    """Scale to fit inside w x h (aspect preserved) and centre on paper."""
    paper = SPECTRA6[0]
    canvas = Image.new("RGB", (w, h), paper)
    avail_w, avail_h = w * (1 - mat), h * (1 - mat)
    s = min(avail_w / img.width, avail_h / img.height)
    nw, nh = max(1, round(img.width * s)), max(1, round(img.height * s))
    canvas.paste(img.convert("RGB").resize((nw, nh), Image.LANCZOS),
                 ((w - nw) // 2, (h - nh) // 2))
    return canvas


def pack_4bpp(p_img: Image.Image, index_map: list[int]) -> bytes:
    """Two pixels per byte, high nibble first, using the panel's indices."""
    table = bytes(index_map[i] if i < len(index_map) else 0 for i in range(256))
    data = p_img.tobytes().translate(table)
    if len(data) % 2:
        data += b"\x00"
    return bytes((data[i] << 4) | data[i + 1] for i in range(0, len(data), 2))


class Renderer:
    """Renders on demand, reusing the last frame while the signature holds."""

    def __init__(self, cfg, index_map):
        self.cfg = cfg
        self.index_map = index_map
        self.lock = threading.Lock()
        self.sig = None
        self.updated = 0
        self.cache = {}  # (w, h, rotate, mat) -> (packed, png)

    def current_signature(self):
        try:
            species = fetch_species(self.cfg, _auth(self.cfg))
            return signature(species), species
        except Exception as e:
            print(f"signature fetch failed: {e}", file=sys.stderr)
            return None, None

    def get(self, w, h, rotate, mat, want_png=False):
        key = (w, h, rotate, mat)
        with self.lock:
            sig, species = self.current_signature()
            heal = time.time() - self.updated >= self.cfg["heal_hours"] * 3600
            fresh = sig is not None and sig == self.sig and key in self.cache and not heal
            if fresh:
                packed, png = self.cache[key]
                return self.sig, packed, png

            img = obtain_image(self.cfg, species)
            if rotate:
                img = img.rotate(rotate, expand=True)
            framed = fit_and_mat(img, w, h, mat)
            p_img = quantize_p(framed)
            packed = pack_4bpp(p_img, self.index_map)

            buf = io.BytesIO()
            p_img.convert("RGB").save(buf, "PNG")
            png = buf.getvalue()

            new_sig = sig if sig is not None else (self.sig or "unknown")
            if new_sig != self.sig:
                self.cache.clear()  # every cached size is stale once birds change
            self.sig = new_sig
            self.updated = time.time()
            self.cache[key] = (packed, png)
            print(f"rendered {w}x{h} rot={rotate} sig={self.sig[:12]} "
                  f"({len(packed)} bytes)")
            return self.sig, packed, png


def make_handler(renderer, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):  # quieter than the default
            print(f"{self.address_string()} {fmt % a}")

        def _params(self):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            def num(name, default, cast=int):
                try:
                    return cast(q[name][0])
                except (KeyError, ValueError):
                    return default
            return (num("w", 800), num("h", 480), num("rotate", 0),
                    num("mat", 0.0, float), q)

        def _deny(self, q):
            if not token:
                return False
            if q.get("token", [""])[0] == token:
                return False
            self.send_error(403, "bad or missing token")
            return True

        def _send(self, body, ctype, etag=None, extra=None):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if etag:
                self.send_header("ETag", etag)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            w, h, rotate, mat, q = self._params()
            if self._deny(q):
                return
            try:
                if path == "/state":
                    sig, packed, _ = renderer.get(w, h, rotate, mat)
                    body = json.dumps({
                        "signature": sig, "updated": int(renderer.updated),
                        "bytes": len(packed), "w": w, "h": h,
                    }).encode()
                    self._send(body, "application/json", etag=sig)
                    return

                if path in ("/frame.bin", "/frame.png"):
                    sig, packed, png = renderer.get(w, h, rotate, mat,
                                                    want_png=(path.endswith(".png")))
                    if self.headers.get("If-None-Match") == sig:
                        self.send_response(304)
                        self.send_header("ETag", sig)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if path == "/frame.bin":
                        self._send(packed, "application/octet-stream", etag=sig,
                                   extra={"X-Frame-Width": str(w),
                                          "X-Frame-Height": str(h),
                                          "X-Frame-Bpp": "4"})
                    else:
                        self._send(png, "image/png", etag=sig)
                    return

                self.send_error(404, "try /state, /frame.bin or /frame.png")
            except Exception as e:
                print(f"error serving {path}: {e}", file=sys.stderr)
                self.send_error(500, str(e))

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="~/.birdframe/config.toml")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--token", default=os.environ.get("FRAME_TOKEN", ""),
                    help="require ?token=... on every request")
    ap.add_argument("--index-map", default=",".join(str(i) for i in DEFAULT_INDEX_MAP),
                    help="panel colour indices for paper,black,red,yellow,blue,green")
    ap.add_argument("--once", metavar="OUT.png",
                    help="render one frame to a file and exit (no server)")
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--height", type=int, default=480)
    args = ap.parse_args()

    try:
        index_map = [int(x) for x in args.index_map.split(",")]
    except ValueError:
        print("error: --index-map must be comma-separated integers", file=sys.stderr)
        return 2
    if len(index_map) < 6:
        print("error: --index-map needs 6 values", file=sys.stderr)
        return 2

    path = os.path.expanduser(args.config)
    if not os.path.exists(path):
        print(f"error: no config at {path} (copy config.example.toml)", file=sys.stderr)
        return 2
    cfg = load_config(path)
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)

    renderer = Renderer(cfg, index_map)

    if args.once:
        _, packed, png = renderer.get(args.width, args.height, 0, cfg.get("mat", 0.0),
                                      want_png=True)
        with open(args.once, "wb") as f:
            f.write(png)
        raw = os.path.splitext(args.once)[0] + ".bin"
        with open(raw, "wb") as f:
            f.write(packed)
        print(f"wrote {args.once} and {raw} ({len(packed)} bytes)")
        return 0

    srv = ThreadingHTTPServer((args.host, args.port), make_handler(renderer, args.token))
    print(f"serving on http://{args.host}:{args.port}  "
          f"(/state, /frame.bin, /frame.png){'  [token required]' if args.token else ''}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
