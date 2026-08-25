#!/usr/bin/env python3
"""Auto-illustrate newly detected birds that are missing illustrations.

Queries the local BirdNET-Pi database for all detected species, checks
which ones lack illustrations, then:
  1. Tries to download from community forks (fork_species.tsv)
  2. Falls back to generating via Gemini (pregen.py)
  3. Runs cutout + mask rebuild for any new images

Designed to run as a cron job on the Pi so new birds get illustrated
automatically within minutes of first detection.

Usage:
    # One-shot (run manually):
    python3 auto_illustrate.py

    # Cron (every 30 minutes):
    */30 * * * * cd ~/BirdNET-Pi/avian/scripts && ~/birdvenv/bin/python3 auto_illustrate.py >> /tmp/auto_illustrate.log 2>&1

Environment variables:
    GEMINI_API_KEY   - required for generating new illustrations
    EBIRD_API_KEY    - not needed (reads detections from local DB)
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ILLUST_DIR = SCRIPT_DIR.parent / "assets" / "illustrations"
FORK_TSV = SCRIPT_DIR / "fork_species.tsv"
APT_JS = SCRIPT_DIR.parent / "frontend" / "apt.js"

DB_PATHS = [
    Path.home() / "BirdNET-Pi" / "scripts" / "birds.db",
    Path("/home/pi/BirdNET-Pi/scripts/birds.db"),
]

PYTHON = sys.executable
LOCKFILE = Path("/tmp/auto_illustrate.lock")


def slugify(sci: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sci.lower()).strip("-")


def find_db() -> Path | None:
    for p in DB_PATHS:
        if p.exists():
            return p
    return None


def get_detected_species(db_path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT DISTINCT Sci_Name, Com_Name FROM detections"
    ).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows if r[0] and r[1]]


def get_missing(species: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    existing = {p.stem for p in ILLUST_DIR.glob("*.png")}
    missing = []
    for sci, com in species:
        slug = slugify(sci)
        if slug not in existing or f"{slug}-2" not in existing:
            missing.append((sci, com, slug))
    return missing


def load_fork_index() -> dict[str, str]:
    if not FORK_TSV.exists():
        return {}
    index = {}
    with open(FORK_TSV) as f:
        next(f)  # skip header
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) >= 3:
                index[parts[0]] = parts[2]  # slug -> base_url
    return index


def download_fork_image(slug: str, base_url: str, suffix: str = "") -> bool:
    fname = f"{slug}{suffix}.png"
    dest = ILLUST_DIR / fname
    if dest.exists():
        return True
    url = f"{base_url}/{fname}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "AvianVisitors/1.0 (auto-illustrate)"
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            if len(data) < 1000:
                return False
            dest.write_bytes(data)
            return True
        except (urllib.error.HTTPError, urllib.error.URLError):
            if attempt < 2:
                time.sleep(2)
                continue
            return False
    return False


def try_fork_download(slug: str, fork_index: dict[str, str]) -> bool:
    if slug not in fork_index:
        return False
    base_url = fork_index[slug]
    pose1 = download_fork_image(slug, base_url, "")
    pose2 = download_fork_image(slug, base_url, "-2")
    return pose1 or pose2


def generate_with_pregen(species_list: list[tuple[str, str]], gemini_key: str) -> int:
    stdin_lines = "\n".join(f"{sci}|{com}" for sci, com in species_list)
    cmd = [PYTHON, str(SCRIPT_DIR / "pregen.py"), "--stdin", "--gemini-key", gemini_key, "--no-refs"]
    result = subprocess.run(cmd, input=stdin_lines, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def run_cutout(slugs: list[str]) -> int:
    cmd = [PYTHON, str(SCRIPT_DIR / "cutout.py"), "--model", "u2net"] + slugs
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def run_build_masks() -> int:
    cmd = [PYTHON, str(SCRIPT_DIR / "build_masks.py")]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def bump_versions():
    if not APT_JS.exists():
        return
    text = APT_JS.read_text()
    import re as _re
    def bump(m):
        prefix, num = m.group(1), int(m.group(2))
        return f"{prefix}{num + 1}"
    new_text = _re.sub(r"(SKETCH_VERSION\s*=\s*'r)(\d+)'", lambda m: bump(m) + "'", text)
    new_text = _re.sub(r"(IMG_VERSION\s*=\s*'r)(\d+)'", lambda m: bump(m) + "'", new_text)
    if new_text != text:
        APT_JS.write_text(new_text)
        print("Bumped SKETCH_VERSION + IMG_VERSION in apt.js")


def main() -> int:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== auto_illustrate [{ts}] ===")

    if LOCKFILE.exists():
        lock_age = time.time() - LOCKFILE.stat().st_mtime
        if lock_age < 600:
            print("Another instance is running (lock < 10 min old). Exiting.")
            return 0
    LOCKFILE.write_text(str(os.getpid()))

    db_path = find_db()
    if not db_path:
        print("error: birds.db not found", file=sys.stderr)
        return 2

    gemini_key = os.environ.get("GEMINI_API_KEY", "")

    species = get_detected_species(db_path)
    print(f"Detected species: {len(species)}")

    ILLUST_DIR.mkdir(parents=True, exist_ok=True)
    missing = get_missing(species)

    if not missing:
        print("All detected species have illustrations.")
        return 0

    print(f"Missing illustrations: {len(missing)} species")

    fork_index = load_fork_index()
    print(f"Fork index: {len(fork_index)} species available")

    from_fork = []
    need_generate = []

    for sci, com, slug in missing:
        if try_fork_download(slug, fork_index):
            from_fork.append((sci, com, slug))
            print(f"  [fork] {com} ({slug})")
        else:
            need_generate.append((sci, com, slug))

    if from_fork:
        print(f"Downloaded {len(from_fork)} species from community forks")

    if need_generate:
        if not gemini_key:
            print(f"warning: {len(need_generate)} species need generation but GEMINI_API_KEY not set",
                  file=sys.stderr)
            print("  Skipping generation. Set GEMINI_API_KEY to enable auto-generation.")
        else:
            print(f"Generating {len(need_generate)} species via Gemini...")
            gen_species = [(sci, com) for sci, com, _ in need_generate]
            generate_with_pregen(gen_species, gemini_key)

    new_slugs = [slug for _, _, slug in from_fork + need_generate]
    if not new_slugs:
        print("No new images to process.")
        return 0

    print(f"\nRunning cutout on {len(new_slugs)} species...")
    slug_args = []
    for slug in new_slugs:
        slug_args.append(slug)
        slug_args.append(f"{slug}-2")
    run_cutout(slug_args)

    print("Rebuilding masks...")
    run_build_masks()

    bump_versions()

    LOCKFILE.unlink(missing_ok=True)
    print(f"\nDone: {len(from_fork)} from forks, {len(need_generate)} generated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
