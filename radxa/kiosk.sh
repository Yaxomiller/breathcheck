#!/usr/bin/env bash
# Opens the BreathCheck UI fullscreen on the device screen.
# Started automatically at desktop login (installed by install.sh),
# or run manually from a terminal on the device.
set -u

ENV_FILE=/etc/breathcheck.env
if [[ -f $ENV_FILE ]]; then
  set -a; . "$ENV_FILE"; set +a
fi
URL="http://127.0.0.1:${HH_WEB_PORT:-8000}/"

# Keep the screen awake and hide the mouse cursor (touchscreen).
if command -v xset >/dev/null 2>&1; then
  xset s off || true
  xset s noblank || true
  xset -dpms || true
fi
if command -v unclutter >/dev/null 2>&1; then
  unclutter -idle 1 -root &
fi

# Wait for the backend service to come up.
echo "Waiting for backend at $URL"
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "$URL"; then
    break
  fi
  sleep 1
done

BROWSER=""
for candidate in chromium chromium-browser google-chrome; do
  if command -v "$candidate" >/dev/null 2>&1; then
    BROWSER=$candidate
    break
  fi
done
if [[ -z $BROWSER ]]; then
  echo "No Chromium browser found — open $URL manually" >&2
  exit 1
fi

# --use-fake-ui-for-media-stream auto-grants the camera permission so the
# exhale photo works unattended (no permission popup on a kiosk).
exec "$BROWSER" \
  --kiosk "$URL" \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-restore-session-state \
  --use-fake-ui-for-media-stream \
  --autoplay-policy=no-user-gesture-required \
  --check-for-update-interval=31536000 \
  --overscroll-history-navigation=0 \
  --pull-to-refresh=0
