#!/usr/bin/env python3
"""Score field recordings submitted from a phone.

Picks up rows the submissions API left as 'pending', normalises the audio
to what BirdNET wants, scores it with the station's own model, and writes
the top candidates back for a human to confirm.

Deliberately the same model, labels and settings the station uses, so a
submission and a station detection mean the same thing. That matters if
the two are ever shown side by side.

The occurrence filter uses the *submission's* coordinates when it has
them, not the station's: a recording made in the highlands should be
judged against highland species. Falls back to the station position.

Nothing here writes to `detections`. A submission stays a submission
until a person confirms it.

Run it as a service, or by hand:

    ~/BirdNET-Pi/birdnet/bin/python3 scripts/submission_worker.py
    ... --once        # drain the queue and exit
    ... --interval 5  # seconds between polls (default 5)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

log = logging.getLogger("submission_worker")

DB_PATH = HERE / "birds.db"
CONF = Path("/etc/birdnet/birdnet.conf")
KEEP_CANDIDATES = 5
# A submission is a deliberate act, so the bar is lower than the station's
# passive threshold - the point is to offer candidates, and the person
# confirming is the real filter.
MIN_CONFIDENCE = 0.05


def conf_value(key: str, fallback: str = "") -> str:
    try:
        for line in CONF.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key}="):
                return stripped.split("=", 1)[1].strip().strip('"') or fallback
    except OSError:
        pass
    return fallback


def to_wav(src: Path, dst: Path) -> bool:
    """Browser audio (webm/opus, mp4/aac) -> 48 kHz mono WAV."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-ac", "1", "-ar", "48000", "-acodec", "pcm_s16le", str(dst)],
        capture_output=True,
    )
    if result.returncode != 0:
        log.warning("ffmpeg failed on %s: %s", src.name,
                    result.stderr.decode("utf-8", "replace")[:200])
        return False
    return dst.exists() and dst.stat().st_size > 0


def analyse(wav: Path, lat: float, lon: float, week: int) -> list[dict]:
    """Top candidates, best first. Imports are local so the module loads
    (and --help works) on a machine without the model."""
    from utils.analysis import readAudioData, analyzeAudioData
    from utils.helpers import get_settings, get_language
    from utils.models import get_model

    conf = get_settings()
    model = get_model()
    overlap = conf.getfloat("OVERLAP")
    names = get_language(conf["DATABASE_LANG"])

    chunks = readAudioData(str(wav), overlap, model.sample_rate, model.chunk_duration)
    raw, _predicted = analyzeAudioData(chunks, overlap, lat, lon, week)

    # Keep the best score per species across the whole clip, rather than
    # per time slot: the submitter cares which bird it was, not when.
    best: dict[str, float] = {}
    for entries in raw.values():
        for sci_name, confidence in entries:
            if confidence < MIN_CONFIDENCE:
                continue
            if "Human" in sci_name or "_" not in sci_name and sci_name.islower():
                continue
            if confidence > best.get(sci_name, 0.0):
                best[sci_name] = float(confidence)

    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    return [
        {"sci": sci, "com": names.get(sci, sci), "conf": round(conf_value_, 4)}
        for sci, conf_value_ in ranked[:KEEP_CANDIDATES]
    ]


def claim_one(db: sqlite3.Connection) -> sqlite3.Row | None:
    """Take the oldest pending row, marking it so a second worker cannot
    take the same one."""
    db.execute("BEGIN IMMEDIATE")
    row = db.execute(
        "SELECT Id, Audio, Lat, Lon, Created FROM submissions "
        "WHERE Status = 'pending' ORDER BY Id LIMIT 1"
    ).fetchone()
    if row is None:
        db.execute("ROLLBACK")
        return None
    db.execute("UPDATE submissions SET Status = 'analysing' WHERE Id = ?", (row["Id"],))
    db.execute("COMMIT")
    return row


def finish(db: sqlite3.Connection, sub_id: int, status: str,
           candidates: list[dict] | None = None, error: str | None = None) -> None:
    db.execute(
        "UPDATE submissions SET Status = ?, Candidates = ?, Error = ? WHERE Id = ?",
        (status, json.dumps(candidates) if candidates is not None else None, error, sub_id),
    )
    db.commit()


def process(db: sqlite3.Connection, row: sqlite3.Row, extracted: Path) -> None:
    sub_id = row["Id"]
    src = extracted / row["Audio"]
    if not src.is_file():
        finish(db, sub_id, "failed", error="the recording is missing")
        return

    # The submission's own position where it has one - the occurrence
    # filter should judge a recording by where it was made.
    lat = row["Lat"] if row["Lat"] is not None else float(conf_value("LATITUDE", "0") or 0)
    lon = row["Lon"] if row["Lon"] is not None else float(conf_value("LONGITUDE", "0") or 0)
    try:
        week = min(48, max(1, int(time.strftime("%m")) * 4 - 3))
    except ValueError:
        week = 1

    tmp = Path(tempfile.mkdtemp(prefix="submission-"))
    try:
        wav = tmp / "clip.wav"
        if not to_wav(src, wav):
            finish(db, sub_id, "failed", error="could not decode the recording")
            return
        candidates = analyse(wav, lat, lon, week)
        if not candidates:
            finish(db, sub_id, "analysed", candidates=[],
                   error="nothing recognisable in this recording")
            log.info("#%s: no candidates", sub_id)
            return
        finish(db, sub_id, "analysed", candidates=candidates)
        log.info("#%s: %s", sub_id, ", ".join(
            f"{c['com']} {c['conf']:.2f}" for c in candidates))
    except Exception as e:                      # a bad clip must not kill the worker
        log.exception("#%s failed", sub_id)
        finish(db, sub_id, "failed", error=str(e)[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="drain the queue and exit")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between polls")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.db.is_file():
        log.error("no database at %s", args.db)
        return 2

    extracted = Path(conf_value("EXTRACTED",
                                str(Path.home() / "BirdSongs" / "Extracted")))

    db = sqlite3.connect(args.db, isolation_level=None, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")

    # The API creates the table on first use; a worker started first
    # should wait rather than fall over.
    have_table = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='submissions'"
    ).fetchone()
    if not have_table:
        if args.once:
            log.info("no submissions table yet - nothing to do")
            return 0
        log.info("waiting for the submissions table to appear")

    while True:
        try:
            row = claim_one(db) if db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='submissions'"
            ).fetchone() else None
        except sqlite3.Error as e:
            log.warning("could not claim a submission: %s", e)
            row = None

        if row is not None:
            process(db, row, extracted)
            continue                      # drain before sleeping
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
