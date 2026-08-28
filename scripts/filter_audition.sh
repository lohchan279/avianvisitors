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
rm -f "$OUT"/*.mp3 "$OUT"/index.html
cards=""

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

  label=$(echo "$name" | sed 's/^[0-9]*-//; s/-/ /g')
  num="${name%%-*}"
  cards+="<section><h2><span class=\"n\">${num}</span>${label}</h2>"
  cards+="<audio controls preload=\"none\" src=\"${name}.mp3\"></audio>"
  cards+="<p class=\"m\">level ${lev} dB · fluctuation ${fl} dB</p>"
  cards+="<code>${chain}</code></section>"
done

# The web server does not offer a directory listing for this folder, so
# write the page that plays them.
{
  cat <<'HTMLHEAD'
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Filter audition</title><style>
:root{color-scheme:light dark;--bg:#fbfaf7;--fg:#1a1a18;--mut:#6b6a65;--line:#dedcd5;--card:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#e9e8e4;--mut:#9a988f;--line:#2e2e34;--card:#1e1e23}}
*{box-sizing:border-box}
body{margin:0;padding:24px 18px 64px;background:var(--bg);color:var(--fg);
font:16px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:640px;margin:0 auto}
h1{font-size:1.35rem;margin:0 0 4px}
.sub{color:var(--mut);font-size:.9rem;margin:0 0 22px}
.how{border-left:3px solid var(--line);padding:2px 0 2px 14px;margin:0 0 26px;
color:var(--mut);font-size:.9rem}
.how b{color:var(--fg)}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;margin:0 0 14px}
h2{font-size:1rem;margin:0 0 10px;display:flex;align-items:center;gap:9px}
.n{display:inline-flex;align-items:center;justify-content:center;width:23px;height:23px;
border-radius:50%;background:var(--fg);color:var(--bg);font-size:.78rem;flex:none}
audio{width:100%;display:block}
.m{color:var(--mut);font-size:.82rem;margin:9px 0 6px}
code{display:block;color:var(--mut);font-size:.74rem;word-break:break-all;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
</style></head><body><main>
<h1>Filter audition</h1>
HTMLHEAD
  echo "<p class=\"sub\">$(basename "$SRC") · first ${MAXLEN}s · floor ${floor} dB</p>"
  cat <<'HTMLMID'
<p class="how">Play <b>1 baseline</b> first — that is what is running now — then
each in turn. Two questions: is the rain-like bed <b>quieter</b>, and do the
calls still sound <b>natural</b> (nothing watery, no smearing on the tail of a
call)? The winner is whichever is quietest without sounding processed. The
numbers are only a hint about where to listen — trust your ears over them.</p>
HTMLMID
  echo "$cards"
  echo "</main></body></html>"
} > "$OUT/index.html"

host=$(hostname 2>/dev/null | tr 'A-Z' 'a-z' || echo ghlyms)
echo
echo "Listen at:  http://${host}.local/filtertest/index.html"
echo "            (or your usual site address, then /filtertest/index.html)"
cat <<'EOF'

That page plays all five in order. Pick whichever is quietest without
sounding processed, and tell me the number.

Remove the files afterwards with:  ./filter_audition.sh --clean
EOF
