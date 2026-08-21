#!/usr/bin/env python3
"""Download matching illustrations from community forks for your region.

Checks your eBird region's species list against illustrations available
in other AvianVisitors forks (UK, Australia, Florida), then downloads
any that match and aren't already in your local illustrations directory.

Usage:
    python3 fetch_fork_illustrations.py --ebird-region SG --ebird-key YOUR_KEY
    python3 fetch_fork_illustrations.py --ebird-region SG --ebird-key YOUR_KEY --dry-run
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ILLUST_DIR = SCRIPT_DIR.parent / "assets" / "illustrations"

FORK_SOURCES = {
    "gb-eng": "https://raw.githubusercontent.com/lloydalexporter/AvianAssets_GB-ENG/main/illustrations",
    "aus-vic": "https://raw.githubusercontent.com/TheWillni/AusVicVisitors/main/illustrations",
    "florida": "https://raw.githubusercontent.com/SupraBitKid/AvianVisitors/avian-visitors/avian/assets/illustrations",
}

FORK_SPECIES: dict[str, str] = {}


def _load_fork_species():
    """Load the embedded fork species mapping."""
    tsv = Path(__file__).with_name("fork_species.tsv")
    if not tsv.exists():
        print(f"error: {tsv} not found", file=sys.stderr)
        sys.exit(2)
    with open(tsv) as f:
        next(f)  # skip header
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) >= 3:
                FORK_SPECIES[parts[0]] = parts[2]  # slug -> base_url


def slugify(sci: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sci.lower()).strip("-")


def fetch_ebird_species(region: str, key: str) -> list[tuple[str, str]]:
    """Fetch species list for an eBird region. Returns [(sci_name, common_name), ...]."""
    spplist_url = f"https://api.ebird.org/v2/product/spplist/{region}"
    req = urllib.request.Request(spplist_url, headers={"X-eBirdApiToken": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        codes = set(json.loads(r.read()))

    tax_url = "https://api.ebird.org/v2/ref/taxonomy/ebird?fmt=json"
    req = urllib.request.Request(tax_url, headers={"X-eBirdApiToken": key})
    print(f"Fetching eBird taxonomy (this takes a moment)...")
    with urllib.request.urlopen(req, timeout=120) as r:
        taxonomy = json.loads(r.read())

    return [(t["sciName"], t["comName"]) for t in taxonomy
            if t["speciesCode"] in codes]


def download(url: str, dest: Path) -> bool:
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "AvianVisitors/1.0 (fork illustration fetch)"
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            dest.write_bytes(data)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            if e.code == 429 and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            return False
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2)
                continue
            return False
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ebird-region", required=True,
                    help="eBird region code (e.g. SG, US-CA, GB-ENG)")
    ap.add_argument("--ebird-key", default=os.environ.get("EBIRD_API_KEY"),
                    help="eBird API key (default: $EBIRD_API_KEY)")
    ap.add_argument("--dir", type=Path, default=ILLUST_DIR,
                    help="Illustration output directory")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be downloaded without downloading")
    args = ap.parse_args()

    if not args.ebird_key:
        print("error: pass --ebird-key or set EBIRD_API_KEY", file=sys.stderr)
        return 2

    _load_fork_species()
    print(f"Loaded {len(FORK_SPECIES)} species from community forks")

    species = fetch_ebird_species(args.ebird_region, args.ebird_key)
    print(f"Found {len(species)} species in eBird region {args.ebird_region}")

    args.dir.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in args.dir.glob("*.png")}

    to_download = []
    already_have = 0
    not_available = 0

    for sci, com in species:
        slug = slugify(sci)
        if slug in existing and f"{slug}-2" in existing:
            already_have += 1
            continue
        if slug in FORK_SPECIES:
            to_download.append((slug, sci, com, FORK_SPECIES[slug]))
        else:
            not_available += 1

    print(f"\nAlready have:       {already_have}")
    print(f"Available in forks: {len(to_download)}")
    print(f"Not in any fork:    {not_available}")

    if args.dry_run:
        print(f"\n[DRY RUN] Would download {len(to_download)} species ({len(to_download)*2} images):")
        for slug, sci, com, _ in to_download[:20]:
            print(f"  {slug}  ({com})")
        if len(to_download) > 20:
            print(f"  ... and {len(to_download) - 20} more")
        return 0

    if not to_download:
        print("\nNothing to download!")
        return 0

    print(f"\nDownloading {len(to_download)} species ({len(to_download)*2} images)...\n")
    ok = failed = 0

    for i, (slug, sci, com, base_url) in enumerate(to_download, 1):
        for suffix in ["", "-2"]:
            fname = f"{slug}{suffix}.png"
            if fname.replace(".png", "") in existing:
                continue
            url = f"{base_url}/{fname}"
            dest = args.dir / fname
            if download(url, dest):
                ok += 1
            else:
                failed += 1

        if i % 10 == 0 or i == len(to_download):
            print(f"  [{i}/{len(to_download)}] {com} ({slug})")
        time.sleep(0.2)

    print(f"\nDone: {ok} images downloaded, {failed} failed")
    if ok > 0:
        print("Run cutout.py and build_masks.py next to process the new images.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
