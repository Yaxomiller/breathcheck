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

# Keep the screen awake. Setting this once at startup is not enough: a
# desktop power manager can re-enable blanking after we run, and DPMS comes
# back on a display hotplug. A road unit that blanks looks like a dead
# device, so reassert it on a timer for as long as the kiosk is up.
keep_awake() {
  while true; do
    xset s off        2>/dev/null || true
    xset s noblank    2>/dev/null || true
    xset -dpms        2>/dev/null || true
    xset dpms 0 0 0   2>/dev/null || true
    sleep "${HH_KEEP_AWAKE_SECONDS:-60}"
  done
}
if command -v xset >/dev/null 2>&1; then
  keep_awake &
  KEEP_AWAKE_PID=$!
  trap 'kill "$KEEP_AWAKE_PID" 2>/dev/null || true' EXIT
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
# How large everything on screen is drawn. 1.0 is the panel's native size;
# 1.25 makes it a quarter bigger. This is a zoom of the whole UI, not a font
# size, so the layout keeps its proportions -- the CSS is already sized in
# viewport units. Set HH_UI_SCALE in /etc/breathcheck.env.
UI_SCALE="${HH_UI_SCALE:-1.0}"

start_firefox() {
  if [[ -z $FIREFOX ]]; then
    echo "No usable browser found — open $URL manually" >&2
    exit 1
  fi
  # Firefox has no scale flag, so the pref goes in a profile we own. A
  # dedicated profile also means no stale .parentlock from an unrelated
  # Firefox window can stop the kiosk starting.
  local profile="${HOME}/.breathcheck-firefox"
  mkdir -p "$profile"
  printf 'user_pref("layout.css.devPixelsPerPx", "%s");\n' "$UI_SCALE" \
    > "$profile/user.js"
  rm -f "$profile/.parentlock" "$profile/lock"
  echo "starting $FIREFOX in kiosk mode (scale $UI_SCALE)"
  # -private-window is Firefox's equivalent of Chromium's --incognito below:
  # without it the frontend is cached across restarts, so a code update looks
  # like it did nothing and the usual response is to reboot, which does not
  # help either.
  exec "$FIREFOX" --kiosk -private-window -profile "$profile" "$URL"
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
echo "starting $CHROMIUM in kiosk mode (scale $UI_SCALE)"
started=$SECONDS
"$CHROMIUM" \
  --kiosk "$URL" \
  --force-device-scale-factor="$UI_SCALE" \
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
