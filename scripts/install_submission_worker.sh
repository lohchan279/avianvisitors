#!/usr/bin/env bash
# Install the field-recording worker as a service.
#
# Kept separate from install_services.sh so this fork does not have to edit
# that file, and so the feature can be added or removed on its own.
#
# The worker shares the station's BirdNET model and settings, so it runs
# under the same interpreter birdnet_analysis uses. It idles at a poll and
# only wakes when a submission is waiting, so the cost when nobody is
# recording is negligible.
#
#   sudo ./scripts/install_submission_worker.sh
#   sudo ./scripts/install_submission_worker.sh --uninstall
#
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME=submission_worker.service
UNIT="$REPO/templates/$UNIT_NAME"

[ "$(id -u)" = 0 ] || { echo "run this with sudo" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now "$UNIT_NAME" 2>/dev/null
  rm -f "/usr/lib/systemd/system/$UNIT_NAME"
  systemctl daemon-reload
  echo "removed $UNIT_NAME"
  exit 0
fi

# Whoever owns the checkout owns the service - matches how the other units
# are installed, and the worker needs to read birds.db and the recordings.
owner=$(stat -c '%U' "$REPO")
[ -n "$owner" ] || { echo "could not determine the repository owner" >&2; exit 1; }

# The interpreter that has the model. Prefer the one birdnet_analysis is
# already running under, so the worker cannot drift onto a different one.
python=''
for candidate in \
  "$(sed -nE 's/.*ExecStart=([^ ]+).*/\1/p' /etc/systemd/system/birdnet_analysis.service 2>/dev/null | head -1)" \
  "$(sed -nE 's/.*ExecStart=([^ ]+).*/\1/p' /usr/lib/systemd/system/birdnet_analysis.service 2>/dev/null | head -1)" \
  "/home/$owner/BirdNET-Pi/birdnet/bin/python3" ; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then python="$candidate"; break; fi
done
[ -n "$python" ] || { echo "could not find BirdNET's python interpreter" >&2; exit 1; }
echo "interpreter: $python"

mkdir -p "$REPO/templates"
cat > "$UNIT" <<EOF
[Unit]
Description=AvianVisitors field-recording worker
After=network.target

[Service]
Type=simple
Restart=always
RestartSec=5
User=$owner
WorkingDirectory=$REPO
ExecStart=$python $REPO/scripts/submission_worker.py
# One clip at a time, and never at the expense of live analysis: the
# station's own detections matter more than a submission waiting a few
# extra seconds.
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=multi-user.target
EOF

ln -sf "$UNIT" "/usr/lib/systemd/system/$UNIT_NAME"
systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"
sleep 1
systemctl --no-pager --lines=5 status "$UNIT_NAME" || true
echo
echo "watch it with:  journalctl -u $UNIT_NAME -f"
