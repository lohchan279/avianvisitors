#!/usr/bin/env bash
# Work out what the steady background noise in a recording actually is.
#
# Broadband hiss has two very different causes that sound identical:
# insects (band-limited, modulated, loudest somewhere in 4-8 kHz) and
# preamp self-noise (flat across every band, perfectly steady). They need
# opposite fixes, so measure before filtering.
#
# Prints the energy in each band, plus how much the level moves second to
# second. Writes nothing and changes no settings.
#
#   ./noise_profile.sh                     # 2 most recent recordings
#   ./noise_profile.sh clip.mp3            # a specific file
#   ./noise_profile.sh quiet.wav loud.wav  # compare two
#
set -uo pipefail

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for tool in sox ffmpeg awk; do
  command -v "$tool" >/dev/null || { echo "error: needs $tool" >&2; exit 2; }
done

# band_lo band_hi label likely-source
BANDS=(
  "20 250 20-250Hz traffic/aircon/wind/mains"
  "250 500 250-500Hz machinery,_voices"
  "500 1000 500Hz-1k koel,_larger_birds"
  "1000 2000 1k-2k most_birdsong"
  "2000 3000 2k-3k most_birdsong"
  "3000 4000 3k-4k songbirds"
  "4000 6000 4k-6k insects_+_high_songbirds"
  "6000 8000 6k-8k insects"
  "8000 11000 8k-11k insects,_hiss"
  "11000 16000 11k-16k hiss"
)

# --- pick inputs -----------------------------------------------------------
if [ "$#" -gt 0 ]; then
  INPUTS=("$@")
else
  CONF=/etc/birdnet/birdnet.conf
  RECS=""; EXTR=""
  if [ -r "$CONF" ]; then
    RECS=$(awk -F= '/^RECS_DIR=/{gsub(/"/,"",$2); print $2}' "$CONF" | tail -1)
    EXTR=$(awk -F= '/^EXTRACTED=/{gsub(/"/,"",$2); print $2}' "$CONF" | tail -1)
  fi
  RECS="${RECS:-$HOME/BirdSongs}"
  EXTR="${EXTR:-$HOME/BirdSongs/Extracted}"
  # Raw recordings first: they contain the ambient bed, not just a call.
  mapfile -t INPUTS < <(ls -t "$RECS"/StreamData/*.wav 2>/dev/null | head -2)
  if [ "${#INPUTS[@]}" -eq 0 ]; then
    mapfile -t INPUTS < <(ls -t "$EXTR"/By_Date/*/*/*.mp3 2>/dev/null | head -2)
  fi
  if [ "${#INPUTS[@]}" -eq 0 ]; then
    echo "error: found no recordings; pass a file explicitly." >&2
    exit 2
  fi
  echo "No files given - using the most recent recordings."
fi

rms_db() {  # file, then any sox effects
  local f="$1"; shift
  sox "$f" -n "$@" stats 2>&1 | awk '/RMS lev dB/{print $4; exit}'
}

for src in "${INPUTS[@]}"; do
  if [ ! -f "$src" ]; then
    echo "skip (not found): $src" >&2
    continue
  fi
  wav="$WORK/in.wav"
  if ! ffmpeg -hide_banner -loglevel error -y -i "$src" \
        -ac 1 -ar 48000 -acodec pcm_s16le "$wav" 2>/dev/null; then
    echo "skip (cannot decode): $src" >&2
    continue
  fi
  dur=$(soxi -D "$wav" 2>/dev/null || echo 0)
  overall=$(rms_db "$wav")

  echo
  echo "=============================================================="
  echo "$(basename "$src")"
  printf "%.1f s   overall RMS %s dB\n" "$dur" "$overall"
  echo "=============================================================="

  # --- steadiness: does the level move moment to moment? ------------------
  # Informational only. Short windows, because a cicada chorus modulates
  # several times a second and one-second windows average it flat.
  win=0.25
  n_win=$(awk -v d="$dur" -v w="$win" 'BEGIN{n=int(d/w); print (n>40?40:n)}')
  if [ "${n_win:-0}" -ge 8 ]; then
    levels=""
    for ((i=0; i<n_win; i++)); do
      off=$(awk -v i="$i" -v w="$win" 'BEGIN{printf "%.2f", i*w}')
      l=$(rms_db "$wav" trim "$off" "$win")
      [ -n "$l" ] && levels+="$l"$'\n'
    done
    spread=$(printf "%s" "$levels" | awk '
      NR==1{mn=mx=$1} {if($1<mn)mn=$1; if($1>mx)mx=$1}
      END{printf "%.1f", mx-mn}')
  else
    spread=""
  fi

  # --- noise floor, and the denoiser setting that follows from it ---------
  # afftdn subtracts the steady part of the spectrum and leaves transients,
  # which is what separates a constant ambient bed from a bird call. Its nf
  # must be set at the actual floor: set it too low and the filter treats
  # the noise as signal and does nothing at all.
  # sox reports the trough - the quietest instant - which sits below the
  # sustained bed, so nf wants a small margin above it. Keep that margin
  # small: measuring separation alone says bigger is always better, but
  # over-declaring the noise makes afftdn carve into the signal, which
  # sounds watery and is audible long before any level metric moves. A
  # margin of 3 dB with nr=12 keeps most of the separation (+12.8 dB
  # against +6.9 unprocessed) at roughly half the residual fluctuation of
  # the aggressive settings.
  floor=$(sox "$wav" -n stats 2>&1 | awk '/RMS Tr dB/{print $4; exit}')
  # nf has to describe what the denoiser will actually see. It runs after the
  # existing highpass/lowpass, so measure inside that band - a raw recording's
  # full-band floor is dominated by the low rumble the highpass already threw
  # away, and would set nf tens of dB too high.
  floor_in=$(sox "$wav" -n sinc 900-10000 stats 2>&1 | awk '/RMS Tr dB/{print $4; exit}')
  suggest=$(awk -v f="${floor_in:-}" 'BEGIN{
      if (f == "") { print ""; exit }
      n = int(f+0.5) + 3; if (n < -80) n = -80; if (n > -20) n = -20;
      printf "%d", n }')

  # --- energy per band ----------------------------------------------------
  # Compared as power *density* (per Hz), not raw band power: the bands are
  # deliberately different widths, and wideband noise puts more power in a
  # wide band purely because it is wide. Density is what makes flat
  # electrical noise look flat.
  rows=""
  for b in "${BANDS[@]}"; do
    read -r lo hi label note <<<"$b"
    db=$(rms_db "$wav" sinc "${lo}-${hi}")
    [ -z "$db" ] && db=-99
    rows+="$label $db $lo $hi $note"$'\n'
  done

  printf "%s" "$rows" | awk -v sp="${spread:-}" -v floor="${floor:-}" \
                            -v floor_in="${floor_in:-}" -v nf="${suggest:-}" '
    function log10(x) { return log(x)/log(10) }
    {
      lab[NR]=$1; db[NR]=$2; lo[NR]=$3; hi[NR]=$4; note[NR]=$5; n=NR
      dens[NR] = $2 - 10*log10($4-$3)          # dB per Hz
      if (n==1 || dens[NR] > peak) { peak=dens[NR]; peaki=NR }
    }
    END {
      if (n==0) { print "  (no data)"; exit }
      printf "\n  %-10s %8s %8s\n", "band", "RMS dB", "rel dB"
      for (i=1; i<=n; i++) {
        rel = dens[i] - peak                   # 0 = densest band
        bars = int((rel+50)/2.5); if (bars<0) bars=0
        bar=""; for (j=0; j<bars; j++) bar=bar "#"
        gsub(/_/, " ", note[i])
        printf "  %-10s %8.1f %8.1f %-21s %s\n", lab[i], db[i], rel, bar, note[i]
      }
      falloff = peak - dens[n]                 # peak vs the top band
      pl=lab[peaki]; gsub(/_/, " ", pl)
      printf "\n  densest band : %s\n", pl
      printf "  high-band falloff : %.1f dB below that at %s\n", falloff, lab[n]
      if (sp != "") printf "  steadiness   : moves %s dB across 0.25 s windows\n", sp
      if (floor != "") printf "  noise floor  : %s dB full band\n", floor
      if (floor_in != "") printf "               : %s dB within 900 Hz-10 kHz\n", floor_in

      # A steady bed inside the bird band cannot be removed by a highpass or
      # a lowpass - it sits on the same frequencies as the birds. Offer the
      # denoiser whenever the noise is acoustic; if it were electrical the
      # flat-density branch below applies instead and gain is the answer.
      if (nf != "" && falloff >= 6) {
        print ""
        print "  steady broadband bed inside the bird band? For the LIVE STREAM"
        print "  only (leave the recordings BirdNET scores alone), try:"
        printf "    LIVESTREAM_FILTER=\"highpass=f=900,lowpass=f=10000,afftdn=nr=12:nf=%s\"\n", nf
        print "  Judge it by ear: no level metric hears the watery artefacts an"
        print "  over-aggressive setting makes. Still watery -> lower nf by 3."
        print "  Not enough effect -> raise nf by 3, or nr to 16."
      }

      print ""
      insect_band = (lo[peaki] >= 3000 && lo[peaki] < 11000)
      if (falloff < 6)
        print "  => density is flat all the way to 16 kHz. That is the signature\n     of electrical self-noise, not sound: real outdoor noise rolls\n     off at the top. Filtering will NOT remove it - lower the capture\n     gain. Confirm with the covered-mic test below."
      else if (falloff >= 12 && insect_band)
        printf "  => a distinct peak at %s that dies away above it: band-limited\n     acoustic noise, which at this frequency means insects/cicadas.\n     A lowpass just above that band will cut it.\n", pl
      else if (falloff >= 12)
        printf "  => concentrated at %s, rolling off above: acoustic, not electrical.\n", pl
      else
        printf "  => neither shape is clean (falloff %.1f dB). Run the covered-mic\n     test below - it settles it in ten seconds.\n", falloff
    }'
done

cat <<'EOF'

--------------------------------------------------------------------------
Confirming it, if the numbers are ambiguous - one physical test settles it:
wrap the microphone in a thick towel, or take it indoors to a quiet room,
and record 10 seconds:

    arecord -D plughw:2,0 -f S16_LE -r 48000 -c 1 -d 10 /tmp/covered.wav
    ./noise_profile.sh /tmp/covered.wav

If the hiss is still there with the mic smothered, it is electrical - it is
being generated after the capsule, and no filter removes it. If it goes
quiet, the hiss is real sound arriving from outside.
--------------------------------------------------------------------------
EOF
