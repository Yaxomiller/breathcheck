#!/usr/bin/env bash
# Pull the latest code and apply it, without rebooting.
#
#   bash ~/Projects/breathcheck/radxa/update.sh
#
# Backend files (src/*.py) need the service restarted. Frontend files
# (frontend/*) are static, so they only need the page reloaded -- but a
# browser already showing the UI will keep serving the old page until it is,
# which is why a pull alone looks like it did nothing. This does both.
#
# Run it as the normal user, not with sudo: restarting the kiosk browser needs
# that user's X session. It calls sudo itself for the service.
set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR" || { echo "cannot find the app at $APP_DIR" >&2; exit 1; }

echo "==> Pulling into $APP_DIR"
# --ff-only: never invent a merge commit on a device, and it silences git's
# divergent-branches hint as a side effect.
if ! git pull --ff-only; then
  echo "    pull failed -- local edits on the device? try: git checkout -- ." >&2
  exit 1
fi

echo "==> Restarting the backend"
sudo systemctl restart breathcheck

echo "==> Reloading the kiosk"
if [[ -n ${DISPLAY:-} ]] || pgrep -x Xorg >/dev/null 2>&1; then
  export DISPLAY="${DISPLAY:-:0}"
  export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"
  pkill -f 'chromium|chromium-browser|google-chrome|firefox' 2>/dev/null || true
  sleep 2
  # No --boot-delay: this is a manual update, the board is already up.
  nohup "$APP_DIR/radxa/kiosk.sh" >/tmp/breathcheck-kiosk.log 2>&1 &
  echo "    kiosk restarting (log: /tmp/breathcheck-kiosk.log)"
else
  echo "    no X session reachable from here -- press F5 on the device"
fi

echo "==> Done.  $(git log --oneline -1)"
