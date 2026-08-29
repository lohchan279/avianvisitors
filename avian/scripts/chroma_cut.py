#!/usr/bin/env python3
"""Cut the cream ground off an illustration without rembg.

cutout.py is the quality path, but it needs rembg + onnxruntime and a
model download, which a BirdNET-Pi venv generally does not have. This
runs the flood-fill cutter from generate_one.py - the one behind the
atlas "generate illustration" button - which needs only Pillow and numpy
and is what the Pi already uses for on-demand renders.

Use it when a render was made by pregen.py (so it is still sitting on its
flat cream ground) and cutout.py is unavailable. The raw render is kept
in illustrations/raw/ and the cut is recorded in cuts.json, so a later
workstation pass (upgrade_cutouts.py) can redo it with BiRefNet.

Usage:
    python3 chroma_cut.py corvus-enca-2
    python3 chroma_cut.py corvus-enca corvus-enca-2 --force
"""
from __future__ import annotations
import argparse
import os
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from generate_one import ILLUS, chroma_cut, record_cut  # noqa: E402


def already_cut(path: Path) -> bool:
    from PIL import Image
    im = Image.open(path)
    # convert() rather than getchannel(): a paletted PNG carries its
    # transparency in the palette, where getchannel("A") cannot see it.
    alpha = im.convert("RGBA").getchannel("A")
    return alpha.getextrema()[0] == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slugs", nargs="+", help="Slugs to cut, e.g. corvus-enca-2")
    ap.add_argument("--dir", type=Path, default=ILLUS)
    ap.add_argument("--force", action="store_true",
                    help="Re-cut even if the image already has transparency")
    ap.add_argument("--keep-raw", action="store_true", default=True,
                    help="Copy the uncut render to illustrations/raw/ first")
    args = ap.parse_args()

    raw_dir = args.dir / "raw"
    done = failed = skipped = 0

    for slug in args.slugs:
        src = args.dir / f"{slug}.png"
        if not src.exists():
            print(f"  [miss] {slug}.png not found", file=sys.stderr)
            failed += 1
            continue
        if not args.force and already_cut(src):
            print(f"  [skip] {slug} already has transparency (--force to redo)")
            skipped += 1
            continue

        # Keep the uncut render: the cut is destructive and the flood-fill
        # is the lower-quality path, so the upgrade pass needs the original.
        if args.keep_raw:
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw = raw_dir / f"{slug}.png"
            if not raw.exists():
                shutil.copy2(src, raw)

        tmp = src.with_suffix(".cut.tmp.png")
        try:
            chroma_cut(src, tmp)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            print(f"  [fail] {slug}: {e}", file=sys.stderr)
            failed += 1
            continue
        os.replace(tmp, src)          # atomic: never leave a half-written png
        record_cut(slug.removesuffix("-2"), "chroma")
        from PIL import Image
        with Image.open(src) as im:
            print(f"  [cut]  {slug} -> {im.width}x{im.height}")
        done += 1

    print(f"\ncut {done} · skipped {skipped} · failed {failed}")
    if done:
        print("next: python3 build_masks.py --add " +
              " ".join(sorted({s.removesuffix('-2') for s in args.slugs})))
    return 1 if failed and not done else 0


if __name__ == "__main__":
    sys.exit(main())
