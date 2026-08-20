#!/usr/bin/env python3
"""Download kachō-e style reference images from Wikimedia Commons.

These Edo-period woodblock prints by Ohara Koson and Hiroshi Yoshida are
used by pregen.py as style anchors so Gemini generates illustrations in
the correct kachō-e aesthetic. All works are public domain (artists died
before 1967, PD-Japan / PD-US).

Run once before pregen.py:
    python3 avian/scripts/fetch_style_refs.py

Images are saved to avian/assets/references/styles/.
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STYLES_DIR = SCRIPT_DIR.parent / "assets" / "references" / "styles"

USER_AGENT = "AvianVisitors/1.0 (style-ref fetch; public-domain kachoe prints)"

# Each entry: (target_filename, wikimedia_search_terms, fallback_file_title)
# fallback_file_title is a known Wikimedia Commons filename to try first.
STYLE_IMAGES = [
    (
        "01-sparrows-on-bamboo-Koson.jpg",
        "Koson sparrows bamboo",
        "File:Koson - sparrows-on-bamboo-tree.jpg",
    ),
    (
        "02-cawing-crow-Koson.jpg",
        "Koson crow blossom",
        "File:Crow and blossom by Ohara Koson.jpg",
    ),
    (
        "03-jays-on-berry-tree-Koson.jpg",
        "Koson jay berry",
        "File:Ohara Koson - Two pigeons on branch of maple.jpg",
    ),
    (
        "04-kingfisher-Koson.jpg",
        "Koson kingfisher",
        "File:Ohara Koson - Kingfisher.jpg",
    ),
    (
        "05-owl-on-ginkgo-Koson.jpg",
        "Koson owl ginkgo",
        "File:Koson - cherry-on-a-moonlit-night-1932.jpg",
    ),
    (
        "06-goose-flying-in-moonlight-Koson.jpg",
        "Koson goose flying moonlight",
        "File:Brooklyn Museum - Goose Flying in Moonlight - Ohara Koson (Shoson).jpg",
    ),
    (
        "07-swallows-in-flight-Koson.jpg",
        "Koson swallows flight",
        "File:Ohara Koson - Barn Swallows in Flight.jpg",
    ),
    (
        "08-crane-in-small-water-Koson.jpg",
        "Koson crane heron water",
        "File:Ohara Shoson (Koson), Egrets in Snow, 1927.jpg",
    ),
    (
        "09-cockatoo-Yoshida.jpg",
        "Yoshida Hiroshi cockatoo parrot",
        "File:Yoshida Hiroshi - Obatan Parrot.jpg",
    ),
    (
        "10-mandarin-ducks-Yoshida.jpg",
        "Yoshida Hiroshi ducks",
        "File:Yoshida Hiroshi - Ducks.jpg",
    ),
]


def _fetch_json(url: str) -> dict | None:
    """GET a JSON URL with retry on 429."""
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 15 * (attempt + 1)
                print(f"    [429] rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"    API error: {e}", file=sys.stderr)
            return None
        except urllib.error.URLError as e:
            print(f"    network error: {e}", file=sys.stderr)
            return None
    return None


def wikimedia_image_url(file_title: str) -> str | None:
    """Get the direct image URL for a Wikimedia Commons file title."""
    title = file_title.replace(" ", "_")
    api = (
        "https://commons.wikimedia.org/w/api.php?"
        + urllib.parse.urlencode({
            "action": "query",
            "titles": title,
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json",
        })
    )
    data = _fetch_json(api)
    if not data:
        return None
    pages = data.get("query", {}).get("pages", {})
    for pid, page in pages.items():
        if int(pid) == -1:
            return None
        ii = page.get("imageinfo", [{}])
        if ii:
            return ii[0].get("url")
    return None


def search_wikimedia(terms: str) -> str | None:
    """Search Wikimedia Commons for a file matching the terms."""
    api = (
        "https://commons.wikimedia.org/w/api.php?"
        + urllib.parse.urlencode({
            "action": "query",
            "list": "search",
            "srnamespace": "6",  # File namespace
            "srsearch": terms,
            "srlimit": "5",
            "format": "json",
        })
    )
    data = _fetch_json(api)
    if not data:
        return None
    results = data.get("query", {}).get("search", [])
    for result in results:
        title = result.get("title", "")
        if title.lower().endswith((".jpg", ".jpeg", ".png")):
            return title
    return None


def download(url: str, dest: Path) -> bool:
    """Download a URL to a local path, with retry on 429."""
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            dest.write_bytes(data)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 15 * (attempt + 1)
                print(f"    [429] rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"    download error: {e}", file=sys.stderr)
            return False
        except urllib.error.URLError as e:
            print(f"    download error: {e}", file=sys.stderr)
            return False
    return False


def main():
    STYLES_DIR.mkdir(parents=True, exist_ok=True)
    ok, skipped, failed = 0, 0, 0

    for target_name, search_terms, fallback_title in STYLE_IMAGES:
        dest = STYLES_DIR / target_name
        if dest.exists() and dest.stat().st_size > 1024:
            print(f"  [skip] {target_name} (already exists)")
            skipped += 1
            continue

        print(f"  [fetch] {target_name} ...")

        # Try the known fallback title first
        url = wikimedia_image_url(fallback_title)
        source = fallback_title

        # If fallback didn't work, search
        if not url:
            print(f"    fallback not found, searching: {search_terms}")
            found_title = search_wikimedia(search_terms)
            if found_title:
                url = wikimedia_image_url(found_title)
                source = found_title

        if not url:
            print(f"    [FAIL] could not find image for {target_name}", file=sys.stderr)
            failed += 1
            continue

        if download(url, dest):
            size_kb = dest.stat().st_size // 1024
            print(f"    -> {target_name} ({size_kb} KB) from {source}")
            ok += 1
        else:
            failed += 1
        time.sleep(3)

    print(f"\nDone: {ok} downloaded, {skipped} already existed, {failed} failed.")
    if failed:
        print("Some images failed — you can place them manually in:")
        print(f"  {STYLES_DIR}/")
        print("Any kachō-e woodblock print JPEG will work as a style anchor.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
