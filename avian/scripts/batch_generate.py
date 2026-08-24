#!/usr/bin/env python3
"""Batch-generate bird illustrations via Google Gemini Batch API (50% cheaper).

Creates a JSONL file of all missing species prompts, uploads it to
Google's File API, submits a batch job, polls for completion, then
downloads and saves the resulting images.

Requires: pip install google-genai

Usage:
    export GEMINI_API_KEY=your-key
    python3 batch_generate.py --ebird-region SG --ebird-key YOUR_KEY
    python3 batch_generate.py --ebird-region SG --ebird-key YOUR_KEY --dry-run
"""
from __future__ import annotations
import argparse
import base64
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
PROMPT_FILE = SCRIPT_DIR / "prompt.template.md"
NOTES_FILE = SCRIPT_DIR / "species-notes.json"

MODEL = "gemini-2.5-flash-image"
POSES = {1: "perched", 2: "in flight with wings spread"}


def slugify(sci: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sci.lower()).strip("-")


def fetch_ebird_species(region: str, key: str) -> list[tuple[str, str]]:
    spplist_url = f"https://api.ebird.org/v2/product/spplist/{region}"
    req = urllib.request.Request(spplist_url, headers={"X-eBirdApiToken": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        codes = set(json.loads(r.read()))

    tax_url = "https://api.ebird.org/v2/ref/taxonomy/ebird?fmt=json"
    req = urllib.request.Request(tax_url, headers={"X-eBirdApiToken": key})
    print("Fetching eBird taxonomy...")
    with urllib.request.urlopen(req, timeout=120) as r:
        taxonomy = json.loads(r.read())

    return [(t["sciName"], t["comName"]) for t in taxonomy
            if t["speciesCode"] in codes]


def load_prompt(path: Path) -> str:
    text = path.read_text()
    marker = "## Prompt"
    idx = text.find(marker)
    if idx >= 0:
        text = text[idx + len(marker):]
    dash = text.find("---")
    if dash >= 0:
        text = text[:dash]
    return text.strip()


def load_notes(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def build_request(prompt: str, sci: str, com: str, pose: int,
                   notes: dict) -> dict:
    body = (prompt
            .replace("{sci_name}", sci)
            .replace("{com_name}", com)
            .replace("{pose}", POSES[pose])
            .replace("{anti_ref_line}", ""))
    note = notes.get(sci)
    if note:
        body += "\n\nSpecies-specific note: " + note

    return {
        "contents": [{"parts": [{"text": body}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ebird-region", required=True)
    ap.add_argument("--ebird-key", default=os.environ.get("EBIRD_API_KEY"))
    ap.add_argument("--gemini-key", default=os.environ.get("GEMINI_API_KEY"))
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--out", type=Path, default=ILLUST_DIR)
    ap.add_argument("--dry-run", action="store_true",
                    help="Build JSONL but don't submit")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not args.gemini_key:
        print("error: GEMINI_API_KEY required (--gemini-key or env)", file=sys.stderr)
        return 2
    if not args.ebird_key:
        print("error: EBIRD_API_KEY required (--ebird-key or env)", file=sys.stderr)
        return 2

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("error: install google-genai first: pip install google-genai", file=sys.stderr)
        return 2

    # Find missing species
    species = fetch_ebird_species(args.ebird_region, args.ebird_key)
    print(f"Found {len(species)} species in {args.ebird_region}")

    args.out.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in args.out.glob("*.png")}

    prompt = load_prompt(PROMPT_FILE)
    notes = load_notes(NOTES_FILE)

    # Build JSONL requests for missing species
    requests = []
    for sci, com in species:
        slug = slugify(sci)
        for pose in [1, 2]:
            fname = f"{slug}.png" if pose == 1 else f"{slug}-2.png"
            stem = fname.replace(".png", "")
            if stem in existing:
                continue
            key = f"{slug}_pose{pose}"
            req = build_request(prompt, sci, com, pose, notes)
            requests.append({"key": key, "request": req})

    if args.limit:
        requests = requests[:args.limit]

    print(f"Missing images: {len(requests)} ({len(requests)//2} species x 2 poses)")

    if not requests:
        print("Nothing to generate!")
        return 0

    # Write JSONL
    jsonl_path = SCRIPT_DIR / "batch_requests.jsonl"
    with open(jsonl_path, "w") as f:
        for r in requests:
            f.write(json.dumps(r) + "\n")
    size_mb = jsonl_path.stat().st_size / 1024 / 1024
    print(f"Wrote {jsonl_path.name} ({size_mb:.1f} MB, {len(requests)} requests)")

    if args.dry_run:
        print(f"\n[DRY RUN] Would submit {len(requests)} requests to batch API")
        print(f"Estimated cost at 50% batch discount:")
        print(f"  ~${len(requests) * 0.02:.2f} USD (at ~$0.02/image)")
        return 0

    # Submit batch job
    client = genai.Client(api_key=args.gemini_key)

    print("Uploading JSONL file...")
    uploaded = client.files.upload(
        file=str(jsonl_path),
        config=types.UploadFileConfig(display_name="avian-batch-input")
    )
    print(f"Uploaded: {uploaded.name}")

    print(f"Submitting batch job ({len(requests)} requests, model={args.model})...")
    batch_job = client.batches.create(
        model=args.model,
        src=uploaded.name,
        config={"display_name": f"avian-{args.ebird_region}-illustrations"}
    )
    print(f"Job created: {batch_job.name}")
    print(f"Status: {batch_job.state.name}")

    # Poll for completion
    print("\nPolling for completion (batch jobs typically finish in <1 hour)...")
    job_name = batch_job.name
    start = time.time()
    while True:
        batch_job = client.batches.get(name=job_name)
        state = batch_job.state.name
        elapsed = int(time.time() - start)
        if state in ("JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED"):
            break
        print(f"  [{elapsed//60}m {elapsed%60}s] {state}")
        time.sleep(60)

    print(f"\nJob finished: {state} (took {elapsed//60}m {elapsed%60}s)")

    if state != "JOB_STATE_SUCCEEDED":
        print(f"error: batch job {state}", file=sys.stderr)
        return 1

    # Download results
    print("Downloading results...")
    result_file = batch_job.dest.file_name
    result_data = client.files.download(file=result_file).decode("utf-8")

    saved = failed = 0
    for line in result_data.splitlines():
        if not line.strip():
            continue
        result = json.loads(line)
        key = result.get("key", "")
        resp = result.get("response", {})

        # Parse slug and pose from key
        parts = key.rsplit("_pose", 1)
        if len(parts) != 2:
            continue
        slug, pose_str = parts
        pose = int(pose_str)
        fname = f"{slug}.png" if pose == 1 else f"{slug}-2.png"

        # Extract image data
        img_data = None
        for cand in resp.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    img_data = base64.b64decode(inline["data"])
                    break
            if img_data:
                break

        if img_data:
            (args.out / fname).write_bytes(img_data)
            saved += 1
        else:
            failed += 1
            print(f"  [no image] {fname}", file=sys.stderr)

    print(f"\nDone: {saved} images saved, {failed} failed")
    if saved > 0:
        print("Run cutout.py and build_masks.py next to process the new images.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
