#!/usr/bin/env bash
# Opens the BreathCheck UI fullscreen on the device screen.
# Started automatically at desktop login (installed by install.sh),
# or run manually from a terminal on the device.
#
#   --boot-delay   wait HH_KIOSK_DELAY_SECONDS first (used by the autostart
#                  entry, so the board settles after boot before the UI loads).
#                  Manual runs start immediately.
set -u

ENV_FILE=/etc/breathcheck.env
if [[ -f $ENV_FILE ]]; then
  set -a; . "$ENV_FILE"; set +a
fi
URL="http://127.0.0.1:${HH_WEB_PORT:-8000}/"

if [[ ${1:-} == "--boot-delay" ]]; then
  delay="${HH_KIOSK_DELAY_SECONDS:-60}"
  echo "boot delay: waiting ${delay}s before starting the kiosk"
  sleep "$delay"
fi

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

CHROMIUM=""
for candidate in chromium chromium-browser google-chrome; do
  if command -v "$candidate" >/dev/null 2>&1; then
    CHROMIUM=$candidate
    break
  fi
done
FIREFOX=""
for candidate in firefox firefox-esr; do
  if command -v "$candidate" >/dev/null 2>&1; then
    FIREFOX=$candidate
    break
  fi
done

# Some Allwinner boards cannot run Chromium at all: it dies instantly with
# SIGILL (exit 132) and the screen just stays black with nothing obvious in
# the logs. Firefox works there, so fall back to it rather than leaving a
# blank kiosk. Chromium stays the default where it runs — it starts faster
# and honours --use-fake-ui-for-media-stream for the exhale photo.
start_firefox() {
  if [[ -z $FIREFOX ]]; then
    echo "No usable browser found — open $URL manually" >&2
    exit 1
  fi
  echo "starting $FIREFOX in kiosk mode"
  exec "$FIREFOX" --kiosk "$URL"
}

if [[ -z $CHROMIUM ]]; then
  echo "No Chromium browser installed; trying Firefox"
  start_firefox
fi

# --use-fake-ui-for-media-stream auto-grants the camera permission so the
# exhale photo works unattended (no permission popup on a kiosk).
# --incognito: never serve a stale cached frontend after an app update.
# --disable-gpu / --disable-gpu-compositing: this board's DRM/GBM driver
# can't satisfy Chromium's hardware buffer allocation (gbm_wrapper
# "Failed to export buffer to dma_buf" errors) — render in software instead,
# which is plenty fast for this plain HTML/CSS kiosk UI.
echo "starting $CHROMIUM in kiosk mode"
started=$SECONDS
"$CHROMIUM" \
  --kiosk "$URL" \
  --incognito \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-restore-session-state \
  --use-fake-ui-for-media-stream \
  --autoplay-policy=no-user-gesture-required \
  --check-for-update-interval=31536000 \
  --overscroll-history-navigation=0 \
  --pull-to-refresh=0 \
  --disable-gpu \
  --disable-gpu-compositing \
  --disable-dev-shm-usage
status=$?

# Only a failure *at startup* means Chromium cannot run here. A non-zero exit
# after the kiosk has been up for a while is a crash or a manual kill, and
# silently swapping browsers then would just be confusing.
if (( status != 0 && SECONDS - started < 15 )); then
  echo "$CHROMIUM exited with status $status after $((SECONDS - started))s" >&2
  echo "(132 = SIGILL: unsupported CPU instruction) — falling back to Firefox" >&2
  start_firefox
fi
exit "$status"
