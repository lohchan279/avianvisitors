#!/usr/bin/env python3
"""Shrink illustration PNGs so the collage loads fast over the tunnel.

The generated kachō-e renders come out of the image model at full
resolution (600-700 KB each). The collage draws them at a few hundred
pixels and the e-ink panel is 1600x1200, so that detail is never seen -
it just costs bandwidth on every page load.

This downscales each PNG to a max dimension and re-encodes it. Where
pngquant is available it also quantizes the palette, which is a big win
on flat-colour artwork like this and preserves alpha.

Dry-run by default: it reports what it would save and changes nothing.
Pass --apply to actually rewrite the files.

    python3 optimize_illustrations.py                 # report only
    python3 optimize_illustrations.py --apply         # rewrite in place
    python3 optimize_illustrations.py --apply --max-dim 1400

IMPORTANT: resizing changes the pixel dimensions the collage packs
against, so after --apply you must rebuild the silhouettes:

    python3 build_masks.py

and bump SKETCH_VERSION + IMG_VERSION in avian/frontend/apt.js.
"""
from __future__ import annotations
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ILLUST_DIR = SCRIPT_DIR.parent / "assets" / "illustrations"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def have_pngquant() -> bool:
    return shutil.which("pngquant") is not None


def optimize_one(src: Path, max_dim: int, colors: int, use_pngquant: bool,
                 out_path: Path) -> tuple[int, int] | None:
    """Write an optimized copy to out_path. Returns (before, after) bytes."""
    from PIL import Image

    before = src.stat().st_size
    try:
        img = Image.open(src)
        img.load()
    except Exception as e:
        print(f"  [skip] {src.name}: unreadable ({e})", file=sys.stderr)
        return None

    if img.mode != "RGBA":
        img = img.convert("RGBA")

    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                         Image.LANCZOS)

    img.save(out_path, "PNG", optimize=True, compress_level=9)

    if use_pngquant:
        tmp = out_path.with_suffix(".quant.png")
        r = subprocess.run(
            ["pngquant", "--force", "--skip-if-larger", "--strip",
             "--quality", "60-95", str(colors), "--output", str(tmp), str(out_path)],
            capture_output=True,
        )
        # exit 98 = "skipped, would be larger"; 99 = quality below floor.
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out_path)
        else:
            tmp.unlink(missing_ok=True)

    return before, out_path.stat().st_size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slugs", nargs="*",
                    help="Specific slugs to process (default: all)")
    ap.add_argument("--dir", type=Path, default=ILLUST_DIR)
    ap.add_argument("--max-dim", type=int, default=1200,
                    help="Longest edge in pixels (default: 1200)")
    ap.add_argument("--colors", type=int, default=128,
                    help="Palette size for pngquant (default: 128)")
    ap.add_argument("--apply", action="store_true",
                    help="Rewrite files. Without this, reports only.")
    ap.add_argument("--no-pngquant", action="store_true",
                    help="Skip palette quantization even if pngquant exists")
    ap.add_argument("--limit", type=int, default=0,
                    help="Only process the first N files (for sampling)")
    args = ap.parse_args()

    try:
        import PIL  # noqa: F401
    except ImportError:
        print("error: needs Pillow (pip install -r requirements.txt)", file=sys.stderr)
        return 2

    if args.slugs:
        files = [args.dir / f"{s}.png" for s in args.slugs]
        files = [f for f in files if f.is_file()]
    else:
        files = sorted(args.dir.glob("*.png"))
    if args.limit:
        files = files[:args.limit]

    if not files:
        print("No PNGs found.")
        return 0

    use_pngquant = have_pngquant() and not args.no_pngquant
    mode = "APPLY (rewriting files)" if args.apply else "DRY RUN (no changes)"
    print(f"{mode} · {len(files)} files · max-dim {args.max_dim}px · "
          f"pngquant {'on' if use_pngquant else 'off'}\n")
    if not use_pngquant and not args.no_pngquant:
        print("note: pngquant not installed - install it for much better savings:")
        print("      sudo apt install pngquant\n")

    total_before = total_after = 0
    changed = skipped = 0

    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        for i, src in enumerate(files, 1):
            out = tmpdir / src.name
            res = optimize_one(src, args.max_dim, args.colors, use_pngquant, out)
            if res is None:
                skipped += 1
                continue
            before, after = res

            # Never let a file get bigger.
            if after >= before:
                total_before += before
                total_after += before
                skipped += 1
                out.unlink(missing_ok=True)
                continue

            total_before += before
            total_after += after
            changed += 1

            if args.apply:
                shutil.move(str(out), str(src))
            else:
                out.unlink(missing_ok=True)

            if i % 100 == 0 or i == len(files):
                pct = (1 - total_after / total_before) * 100 if total_before else 0
                print(f"  [{i}/{len(files)}] running total: "
                      f"{human(total_before)} -> {human(total_after)}  ({pct:.0f}% smaller)")

    saved = total_before - total_after
    pct = (saved / total_before * 100) if total_before else 0
    print(f"\n{changed} optimized, {skipped} left alone")
    print(f"{human(total_before)} -> {human(total_after)}   saved {human(saved)} ({pct:.0f}%)")

    if not args.apply:
        print("\nThis was a dry run. Re-run with --apply to rewrite the files.")
    else:
        print("\nNext: rebuild silhouettes and bust the cache:")
        print("  python3 build_masks.py")
        print("  then bump SKETCH_VERSION + IMG_VERSION in avian/frontend/apt.js")
    return 0


if __name__ == "__main__":
    sys.exit(main())
