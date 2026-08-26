#!/usr/bin/env bash
# A/B test audio filtering against the real BirdNET model.
#
# Builds filtered variants of a recording and scores each one, so you can
# see which (if any) filter BirdNET actually prefers before changing the
# recording pipeline. Writes nothing to birds.db.
#
#   ./filter_test.sh                          # picks 3 recent detections
#   ./filter_test.sh /path/to/clip.mp3        # a specific recording
#   ./filter_test.sh a.mp3 b.mp3 c.wav        # several
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- find BirdNET's interpreter (NOT the illustration venv) ----------------
PY=""
UNIT=/etc/systemd/system/birdnet_analysis.service
if [ -r "$UNIT" ]; then
  PY=$(grep -h 'ExecStart' "$UNIT" | sed -E 's/.*ExecStart=([^ ]+).*/\1/' | head -1)
fi
for cand in "$PY" "$HOME/BirdNET-Pi/birdnet/bin/python3" "$HOME/birdnet/bin/python3"; do
  if [ -n "$cand" ] && [ -x "$cand" ] && "$cand" -c 'import librosa' 2>/dev/null; then
    PY="$cand"; break
  fi
  PY=""
done
if [ -z "$PY" ]; then
  echo "error: could not find BirdNET's python (the one with librosa)." >&2
  echo "  check: grep -h ExecStart $UNIT" >&2
  exit 2
fi
echo "Using interpreter: $PY"

# --- pick inputs -----------------------------------------------------------
if [ "$#" -gt 0 ]; then
  INPUTS=("$@")
else
  mapfile -t INPUTS < <(ls -t "$HOME"/BirdSongs/Extracted/By_Date/*/*/*.mp3 2>/dev/null | head -3)
  if [ "${#INPUTS[@]}" -eq 0 ]; then
    echo "error: no recordings found; pass a file explicitly." >&2
    exit 2
  fi
  echo "No files given - using the 3 most recent detections."
fi

# --- filter variants to compare -------------------------------------------
# name:sox-effect   ("none" = unfiltered reference)
VARIANTS=(
  "orig:none"
  "hp500:highpass 500"
  "hp1000:highpass 1000"
  "hp1500:highpass 1500"
  "band1000_8000:highpass 1000 lowpass 8000"
)

for src in "${INPUTS[@]}"; do
  if [ ! -f "$src" ]; then
    echo "skip (not found): $src" >&2
    continue
  fi
  base=$(basename "${src%.*}" | tr -c 'A-Za-z0-9_-' '_')
  echo
  echo "########################################################"
  echo "# $(basename "$src")"
  echo "########################################################"

  ref="$WORK/${base}__ref.wav"
  if ! ffmpeg -hide_banner -loglevel error -y -i "$src" \
        -ac 1 -ar 48000 -acodec pcm_s16le "$ref" 2>/dev/null; then
    echo "  ffmpeg could not decode this file; skipping." >&2
    continue
  fi

  files=()
  for v in "${VARIANTS[@]}"; do
    name="${v%%:*}"; fx="${v#*:}"
    out="$WORK/${base}__${name}.wav"
    if [ "$fx" = "none" ]; then
      cp "$ref" "$out"
    elif ! sox "$ref" "$out" $fx 2>/dev/null; then
      echo "  sox failed for '$fx'; skipping that variant." >&2
      continue
    fi
    # SNR for context: peak minus noise-floor trough, both in dB.
    read -r pk tr < <(sox "$out" -n stats 2>&1 \
      | awk '/Pk lev dB/{p=$4} /RMS Tr dB/{t=$4} END{print p, t}')
    if [ -n "${pk:-}" ] && [ -n "${tr:-}" ]; then
      printf "  %-14s SNR %5.1f dB  (peak %s, floor %s)\n" \
        "$name" "$(echo "$pk - $tr" | bc -l 2>/dev/null || echo 0)" "$pk" "$tr"
    fi
    files+=("$out")
  done

  echo
  "$PY" "$SCRIPT_DIR/score_file.py" "${files[@]}"
done

echo
echo "Compare the confidence for the SAME species across variants."
echo "If nothing beats 'orig' by more than a few points, filtering will not help."
