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

# Where the settings panel puts the Gemini key. generate.php reads
# $BIRDNETPI_DIR/birdnet.conf, which on a station is a symlink to the file
# in /etc; both are listed so this works either way round.
CONF_PATHS = [
    SCRIPT_DIR.parent.parent / "birdnet.conf",
    Path("/etc/birdnet/birdnet.conf"),
]

PYTHON = sys.executable
LOCKFILE = Path("/tmp/auto_illustrate.lock")


def slugify(sci: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sci.lower()).strip("-")


def gemini_key() -> str:
    """The key, from the environment or from birdnet.conf.

    Reading only os.environ is what kept this from ever generating
    anything. The key is saved by the settings panel into birdnet.conf,
    and generate.php reads it from there and injects it into the
    environment of the process it starts. Nothing does that for this
    script: it is spawned by birdnet_analysis, whose service environment
    has never carried the key. So the automatic path quietly downgraded
    to fork-downloads-only and said so in a log in /tmp, while the manual
    button on the same station worked perfectly.

    Parsed the way conf_value() in generate.php parses it, including the
    optional quotes, so the two cannot disagree about what the file says.
    """
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    for conf in CONF_PATHS:
        try:
            lines = conf.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            m = re.match(r"\s*GEMINI_API_KEY\s*=\s*(.*)$", line)
            if not m:
                continue
            value = m.group(1).strip()
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            if value:
                return value
    return ""


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


def generate_with_pregen(species_list: list[tuple[str, str]], key: str) -> int:
    stdin_lines = "\n".join(f"{sci}|{com}" for sci, com in species_list)
    cmd = [PYTHON, str(SCRIPT_DIR / "pregen.py"), "--stdin", "--no-refs"]
    # Through the environment rather than --gemini-key, which pregen's own
    # usage calls the preferred way and which keeps the key out of the
    # process list: argv is world-readable in /proc, so any local account
    # could read it off ps for as long as a generation runs - and a
    # generation can run for minutes while it waits out a rate limit.
    # generate.php already hands it over this way.
    environment = dict(os.environ, GEMINI_API_KEY=key)
    result = subprocess.run(cmd, input=stdin_lines, capture_output=True,
                            text=True, env=environment)
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
    """Make the new bird visible to browsers that already have the site.

    Two tokens, two jobs, and a new species needs both moved:

      - TABLE_VERSION gates dims.json and masks.json, and dims.json is
        exactly what the atlas consults to decide whether a species has
        art ("needsArt = !DIMS[slug]"). Leave it and every browser keeps
        a table with no entry for the new bird, so the card stays on the
        nest placeholder no matter how often it is reloaded.

      - the ?v= token in index.html gates apt.js itself, and TABLE_VERSION
        lives inside apt.js. Bumping it in a file nobody refetches
        changes nothing at all.

    This used to bump SKETCH_VERSION and IMG_VERSION instead. Those stopped
    being literals when they were changed to read ASSET_VERSION off apt.js's
    own script tag - so the site token already carries them, and the two
    substitutions here had been matching nothing ever since. Neither of the
    tokens that actually matter was being touched.
    """
    moved = []

    if APT_JS.exists():
        text = APT_JS.read_text()
        new_text, hits = re.subn(
            r"(TABLE_VERSION\s*=\s*'r)(\d+)'",
            lambda m: f"{m.group(1)}{int(m.group(2)) + 1}'", text)
        if hits:
            APT_JS.write_text(new_text)
            moved.append("TABLE_VERSION in apt.js")
        else:
            print("warning: TABLE_VERSION not found in apt.js - dims.json will "
                  "stay cached and the new bird will keep its placeholder",
                  file=sys.stderr)

    bumper = SCRIPT_DIR / "bump_version.sh"
    if bumper.exists():
        result = subprocess.run([str(bumper)], capture_output=True, text=True)
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode == 0:
            moved.append("the site cache token")
        else:
            print(f"warning: {bumper.name} failed ({result.returncode}); browsers "
                  f"will keep the old apt.js", file=sys.stderr)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)

    if moved:
        print("Bumped " + " and ".join(moved))


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

    key = gemini_key()

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

    generated = []
    if need_generate:
        if not key:
            print(f"warning: {len(need_generate)} species need generation but "
                  f"no GEMINI_API_KEY in the environment or birdnet.conf",
                  file=sys.stderr)
            print("  Skipping generation. Save the key in Settings to enable it.")
        else:
            print(f"Generating {len(need_generate)} species via Gemini...")
            gen_species = [(sci, com) for sci, com, _ in need_generate]
            generate_with_pregen(gen_species, key)
            # Only count species whose files actually landed - pregen can fail
            # per-species (safety filters, quota) and still exit 0.
            generated = [(sci, com, slug) for sci, com, slug in need_generate
                         if (ILLUST_DIR / f"{slug}.png").exists()]
            if len(generated) < len(need_generate):
                print(f"  {len(need_generate) - len(generated)} species produced no image")

    # Cut only files that exist on disk; asking cutout.py for a missing
    # slug is an error, and pose 2 is frequently absent.
    slug_args = []
    for _, _, slug in from_fork + generated:
        for name in (slug, f"{slug}-2"):
            if (ILLUST_DIR / f"{name}.png").exists():
                slug_args.append(name)

    if not slug_args:
        print("No new images to process.")
        LOCKFILE.unlink(missing_ok=True)
        return 0

    print(f"\nRunning cutout on {len(slug_args)} images...")
    run_cutout(slug_args)

    print("Rebuilding masks...")
    run_build_masks()

    # Only bust caches when pixels actually changed.
    bump_versions()

    LOCKFILE.unlink(missing_ok=True)
    print(f"\nDone: {len(from_fork)} from forks, {len(generated)} generated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
