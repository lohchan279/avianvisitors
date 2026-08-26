#!/usr/bin/env python3
"""Score audio files with BirdNET without touching the database.

Runs the same analyzer the live pipeline uses, but only prints results -
nothing is written to birds.db and no recordings are moved or deleted.
Intended for A/B tests: filter a recording several different ways, score
each variant, and see which one BirdNET actually prefers.

    # score one file
    python3 score_file.py /path/to/clip.wav

    # compare a filtered variant against the original
    sox clip.wav hp800.wav highpass 800
    python3 score_file.py clip.wav hp800.wav

Files are copied to a conforming temporary name before analysis, so the
input can be named anything (ParseFileName requires a date prefix and a
HH:MM:SS suffix, which ad-hoc test files rarely have).

Anything ffmpeg can decode works as input; non-WAV files are converted
to 48 kHz mono WAV first.
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# A name ParseFileName accepts: leading date, trailing HH:MM:SS.
STAMP = "2000-01-01-birdnet-00:00:00"


def to_wav(src: Path, dest: Path) -> bool:
    """Copy src to dest as 48 kHz mono 16-bit WAV."""
    if src.suffix.lower() == ".wav":
        shutil.copy(src, dest)
        return True
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-ac", "1", "-ar", "48000", "-acodec", "pcm_s16le", str(dest)],
        capture_output=True,
    )
    if r.returncode != 0:
        print(f"  ffmpeg failed: {r.stderr.decode()[:200]}", file=sys.stderr)
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path, help="Audio files to score")
    ap.add_argument("--top", type=int, default=5,
                    help="Show this many detections per file (default: 5)")
    args = ap.parse_args()

    missing = [f for f in args.files if not f.is_file()]
    if missing:
        print("error: not found: " + ", ".join(str(m) for m in missing), file=sys.stderr)
        return 2

    try:
        from utils.analysis import run_analysis, load_global_model
        from utils.classes import ParseFileName
    except ImportError as e:
        print(f"error: run this from the BirdNET-Pi scripts/ directory ({e})",
              file=sys.stderr)
        return 2

    print("Loading model...")
    load_global_model()

    for src in args.files:
        print(f"\n=== {src.name} ===")
        with tempfile.TemporaryDirectory() as td:
            staged = Path(td) / f"{STAMP}.wav"
            if not to_wav(src, staged):
                continue
            try:
                detections = run_analysis(ParseFileName(str(staged)))
            except Exception as e:
                print(f"  analysis failed: {e}", file=sys.stderr)
                continue

        if not detections:
            print("  (no detections above the confidence threshold)")
            continue

        ranked = sorted(detections, key=lambda d: -float(d.confidence))
        for d in ranked[:args.top]:
            print(f"  {float(d.confidence)*100:5.1f}%  {d.common_name} "
                  f"({d.scientific_name})  @{d.start:.0f}-{d.stop:.0f}s")
        if len(ranked) > args.top:
            print(f"  ... and {len(ranked) - args.top} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
