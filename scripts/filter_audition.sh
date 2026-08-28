#!/usr/bin/env bash
# Render the same recording through several filter chains so they can be
# compared by ear, back to back, on identical audio.
#
# Tuning a denoiser against the live stream does not work: the birds and
# the traffic differ between one trial and the next, so nothing is being
# compared. And no level metric hears the watery artefact that an
# over-aggressive setting produces - only ears do. This makes the ears a
# fair test.
#
# Writes mp3s into a folder the web server already serves, and prints the
# URLs. Changes no settings and touches no recording.
#
#   ./filter_audition.sh                 # picks a recent detection
#   ./filter_audition.sh clip.mp3        # a specific recording
#   ./filter_audition.sh --clean         # remove the rendered files
#
set -uo pipefail

CONF=/etc/birdnet/birdnet.conf
EXTR=""
[ -r "$CONF" ] && EXTR=$(awk -F= '/^EXTRACTED=/{gsub(/"/,"",$2); print $2}' "$CONF" | tail -1)
EXTR="${EXTR:-$HOME/BirdSongs/Extracted}"
OUT="$EXTR/filtertest"
MAXLEN=20   # seconds - long enough to judge, short enough to re-listen

if [ "${1:-}" = "--clean" ]; then
  rm -rf "$OUT" && echo "removed $OUT"
  exit 0
fi

for tool in ffmpeg sox awk; do
  command -v "$tool" >/dev/null || { echo "error: needs $tool" >&2; exit 2; }
done

# --- pick the input --------------------------------------------------------
if [ "$#" -gt 0 ]; then
  SRC="$1"
else
  # A clip with a bird in it is the right test - the point is to hear what
  # the filter does to a call, not only what it does to the background.
  SRC=$(ls -t "$EXTR"/By_Date/*/*/*.mp3 2>/dev/null | head -1)
  [ -z "$SRC" ] && SRC=$(ls -t "$HOME"/BirdSongs/StreamData/*.wav 2>/dev/null | head -1)
fi
if [ -z "${SRC:-}" ] || [ ! -f "$SRC" ]; then
  echo "error: no recording found; pass one explicitly." >&2
  exit 2
fi

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
ref="$WORK/ref.wav"
if ! ffmpeg -hide_banner -loglevel error -y -i "$SRC" -t "$MAXLEN" \
      -ac 1 -ar 48000 -acodec pcm_s16le "$ref" 2>/dev/null; then
  echo "error: could not decode $SRC" >&2
  exit 2
fi

# nf must describe what the denoiser sees, which is the band left after the
# highpass and lowpass - not the full-band floor, which the rumble sets.
floor=$(sox "$ref" -n sinc 900-10000 stats 2>&1 | awk '/RMS Tr dB/{print $4; exit}')
nf3=$(awk -v f="$floor" 'BEGIN{n=int(f+0.5)+3; if(n<-80)n=-80; if(n>-20)n=-20; print n}')
nf6=$(awk -v f="$floor" 'BEGIN{n=int(f+0.5)+6; if(n<-80)n=-80; if(n>-20)n=-20; print n}')

BAND="highpass=f=900,lowpass=f=10000"
CANDIDATES=(
  "1-baseline|$BAND"
  "2-afftdn-mild|$BAND,afftdn=nr=12:nf=$nf3"
  "3-afftdn-strong|$BAND,afftdn=nr=16:nf=$nf6"
  "4-nlmeans-mild|$BAND,anlmdn=s=0.02"
  "5-nlmeans-strong|$BAND,anlmdn=s=0.05"
)

mkdir -p "$OUT" || { echo "error: cannot write $OUT" >&2; exit 1; }
rm -f "$OUT"/*.mp3

echo "source : $(basename "$SRC")  (first ${MAXLEN}s)"
echo "floor  : $floor dB within 900 Hz-10 kHz"
echo
printf "%-18s %8s %8s  %s\n" "file" "level" "fluct" "chain"

for c in "${CANDIDATES[@]}"; do
  name="${c%%|*}"; chain="${c#*|}"
  wav="$WORK/$name.wav"
  if ! ffmpeg -hide_banner -loglevel error -y -i "$ref" -af "$chain" "$wav" 2>/dev/null; then
    printf "%-18s %8s %8s  %s\n" "$name" "-" "FAILED" "$chain"
    continue
  fi
  ffmpeg -hide_banner -loglevel error -y -i "$wav" -codec:a libmp3lame -b:a 128k \
      "$OUT/$name.mp3" 2>/dev/null \
    || cp "$wav" "$OUT/$name.wav"

  lev=$(sox "$wav" -n stats 2>&1 | awk '/RMS lev dB/{print $4}')
  # Residual fluctuation: steady noise that starts wobbling is the
  # fingerprint of a denoiser reaching too far. Rising numbers are a hint
  # to listen closely, not a verdict - trust your ears over this column.
  # Measured over the quietest quarter of the windows only. Across the whole
  # file this number just tracks how loudly the birds sang; the gaps between
  # calls are where a denoiser's damage shows.
  fl=$(for i in $(seq 0 $((MAXLEN*4-1))); do o=$(awk -v i="$i" 'BEGIN{printf "%.2f", i*0.25}')
        sox "$wav" -n trim "$o" 0.25 stats 2>&1 | awk '/RMS lev dB/{print $4}'; done \
       | grep -v '^-inf' | sort -n \
       | awk '{v[NR]=$1} END{
             q=int(NR/4); if (q<3) q=NR;
             mn=v[1]; mx=v[q];
             printf "%.1f", mx-mn }')
  printf "%-18s %8s %8s  %s\n" "$name" "$lev" "$fl" "$chain"
done

host=$(hostname 2>/dev/null || echo ghlyms)
echo
echo "Listen at:  http://${host}.local/filtertest/"
echo "            (or your usual site address, then /filtertest/)"
cat <<'EOF'

Play 1-baseline first, then each in turn. What to listen for:
  - is the rain-like bed quieter?
  - do the bird calls still sound natural - no watery or underwater
    quality, no smearing on the tail of a call?
The best one is whichever is quietest WITHOUT sounding processed. Tell me
the number and it becomes the LIVESTREAM_FILTER setting.

Remove the files afterwards with:  ./filter_audition.sh --clean
EOF
