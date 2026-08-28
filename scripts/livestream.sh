#!/usr/bin/env bash
# Live Audio Stream Service Script
source /etc/birdnet/birdnet.conf

# Read the logging level from the configuration option
LOGGING_LEVEL="${LogLevel_LiveAudioStreamService}"
# If empty for some reason default to log level of error
[ -z $LOGGING_LEVEL ] && LOGGING_LEVEL='error'
# Additionally if we're at debug or info level then allow printing of script commands and variables
if [ "$LOGGING_LEVEL" == "info" ] || [ "$LOGGING_LEVEL" == "debug" ];then
  # Enable printing of commands/variables etc to terminal for debugging
  set -x
fi

# The stream's audio filters. ffmpeg honours only the LAST -af, so the
# frequency shift and any station filter have to be composed into one
# chain rather than passed as two flags - otherwise setting one silently
# disables the other.
FILTER_CHAIN=""
if [ "$ACTIVATE_FREQSHIFT_IN_LIVESTREAM" == "true" ]; then
  FILTER_CHAIN="rubberband=pitch=${FREQSHIFT_LO}/${FREQSHIFT_HI}"
fi

# LIVESTREAM_FILTER is an optional ffmpeg filter chain applied to the LIVE
# STREAM ONLY. The recordings BirdNET analyses come from a separate arecord
# pipeline and are untouched, so this changes what a listener hears without
# affecting detection. Useful where the microphone is omnidirectional and
# ambient noise sits outside the birds' band, e.g.
#   LIVESTREAM_FILTER="highpass=f=900,lowpass=f=10000"
if [ -n "${LIVESTREAM_FILTER:-}" ]; then
  FILTER_CHAIN="${FILTER_CHAIN:+$FILTER_CHAIN,}${LIVESTREAM_FILTER}"
fi

FREQSHIFT_OPT=""
if [ -n "$FILTER_CHAIN" ]; then
  FREQSHIFT_OPT="-af $FILTER_CHAIN"
fi

if [ -z ${REC_CARD} ];then
  echo "Stream not supported"
elif [[ ! -z ${RTSP_STREAM} ]];then
  # Explode the RSPT steam setting into an array so we can count the number we have
  RSTP_STREAMS_EXPLODED_ARRAY=(${RTSP_STREAM//,/ })

  # If for some reason the RTSP_STREAM_TO_LIVESTREAM is not set, then init it to 0 to use the first stream
  if [[ -z ${RTSP_STREAM_TO_LIVESTREAM} ]];then
    RTSP_STREAM_TO_LIVESTREAM=0
  fi

  # Get the RSTP stream at the specified array index
  SELECTED_RSTP_STREAM=${RSTP_STREAMS_EXPLODED_ARRAY[RTSP_STREAM_TO_LIVESTREAM]}

  # If for some reason the RTSP stream url is null
  if [[ -z ${SELECTED_RSTP_STREAM} ]];then
    # Try select the first stream
    SELECTED_RSTP_STREAM=${RSTP_STREAMS_EXPLODED_ARRAY[0]}
  fi

  ffmpeg -nostdin -loglevel $LOGGING_LEVEL -ac ${CHANNELS} -i ${SELECTED_RSTP_STREAM} -acodec libmp3lame \
    -b:a 320k -ac ${CHANNELS} -content_type 'audio/mpeg' \
    ${FREQSHIFT_OPT} \
    -f mp3 icecast://source:${ICE_PWD}@localhost:8000/stream -re
else
	ffmpeg -nostdin -loglevel $LOGGING_LEVEL -ac ${CHANNELS} -f alsa -i ${REC_CARD} -acodec libmp3lame \
    -b:a 320k -ac ${CHANNELS} -content_type 'audio/mpeg' \
    ${FREQSHIFT_OPT} \
    -f mp3 icecast://source:${ICE_PWD}@localhost:8000/stream -re
fi
