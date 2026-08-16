#!/usr/bin/env bash
# Make BreathCheck the app that owns the Radxa screen.
#
#   bash radxa/set-default-ui.sh
#
# - starts the backend on boot (systemd)
# - launches our kiosk when the desktop session logs in
# - moves any OTHER autostart entry aside, reversibly, so a vendor demo UI
#   stops taking the screen
# - reports anything still competing (e.g. another app holding our port)
#
# Undo: bash radxa/set-default-ui.sh --undo
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE=/etc/breathcheck.env
if [[ -f $ENV_FILE ]]; then
  set -a; . "$ENV_FILE"; set +a
fi
PORT="${HH_WEB_PORT:-8000}"
URL="http://127.0.0.1:${PORT}/"

if [[ $EUID -eq 0 && -n ${SUDO_USER:-} ]]; then
  KIOSK_USER="$SUDO_USER"
else
  KIOSK_USER="$(id -un)"
fi
KIOSK_HOME="$(getent passwd "$KIOSK_USER" | cut -d: -f6)"
AUTOSTART="$KIOSK_HOME/.config/autostart"
DISABLED="$KIOSK_HOME/.config/autostart-disabled"
OURS=breathcheck-kiosk.desktop

# ---------------------------------------------------------------- undo ----
if [[ ${1:-} == "--undo" ]]; then
  echo "==> Restoring the previous default UI"
  rm -f "$AUTOSTART/$OURS"
  sudo rm -f /etc/systemd/system/breathcheck.service.d/boot-delay.conf
  sudo systemctl daemon-reload 2>/dev/null || true
  if [[ -d $DISABLED ]]; then
    for entry in "$DISABLED"/*.desktop; do
      [[ -e $entry ]] || continue
      mv "$entry" "$AUTOSTART/$(basename "$entry")"
      echo "    restored $(basename "$entry")"
    done
  fi
  sudo systemctl disable breathcheck.service || true
  echo "==> Done. BreathCheck no longer autostarts (it can still be started by hand)."
  exit 0
fi

echo "==> Making BreathCheck the default UI"
echo "    user $KIOSK_USER, port $PORT"

# 1. Backend starts on boot and is running now, after a settling delay.
echo "==> Backend service"
BOOT_DELAY="${HH_STARTUP_DELAY_SECONDS:-60}"
sudo mkdir -p /etc/systemd/system/breathcheck.service.d
# Drop-in rather than rewriting the unit, so the installer stays the owner of
# it. The delay only applies at boot: a manual `systemctl start` waits too,
# but that is rare compared to the value of letting the board settle first.
sudo tee /etc/systemd/system/breathcheck.service.d/boot-delay.conf >/dev/null <<UNIT
[Service]
ExecStartPre=/bin/bash -c 'if [ -f /run/breathcheck-booted ]; then exit 0; fi; \\
  sleep ${BOOT_DELAY}; touch /run/breathcheck-booted'
UNIT
echo "    boot delay: ${BOOT_DELAY}s before the backend starts"
sudo systemctl daemon-reload
sudo touch /run/breathcheck-booted     # this run is manual: do not wait now
sudo systemctl enable breathcheck.service
sudo systemctl restart breathcheck.service

# 2. Our kiosk starts with the desktop session.
echo "==> Kiosk autostart"
mkdir -p "$AUTOSTART"
chmod +x "$APP_DIR/radxa/kiosk.sh" "$APP_DIR/radxa/run.sh" 2>/dev/null || true
# Exec runs the script through bash: a fresh clone can land without the
# executable bit, and a .desktop entry pointing at a non-executable file
# fails silently at login.
cat > "$AUTOSTART/$OURS" <<DESKTOP
[Desktop Entry]
Type=Application
Name=BreathCheck Kiosk
Comment=Police handheld breath analyzer
Exec=/bin/bash $APP_DIR/radxa/kiosk.sh --boot-delay
Terminal=false
X-GNOME-Autostart-enabled=true
DESKTOP
echo "    installed $AUTOSTART/$OURS"

# 3. Move competing autostart entries aside (reversible, see --undo).
mkdir -p "$DISABLED"
moved=0
for entry in "$AUTOSTART"/*.desktop; do
  [[ -e $entry ]] || continue
  name="$(basename "$entry")"
  [[ $name == "$OURS" ]] && continue
  mv "$entry" "$DISABLED/$name"
  echo "    disabled other autostart: $name"
  moved=$((moved + 1))
done
[[ $moved -eq 0 ]] && echo "    no competing autostart entries found"

# 4. Wait for the backend, then report anything still in the way.
echo "==> Checking the backend"
ok=0
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "$URL"; then ok=1; break; fi
  sleep 1
done

if [[ $ok -eq 1 ]]; then
  echo "    backend OK at $URL"
else
  echo "    !! backend is NOT answering at $URL"
  echo "    service state: $(systemctl is-active breathcheck || true)"
  holder="$(sudo ss -tlnp 2>/dev/null | grep ":$PORT " || true)"
  if [[ -n $holder ]]; then
    echo "    something else is holding port $PORT:"
    echo "      $holder"
    echo "    -> free that port, or pick another:"
    echo "       sudo sed -i '/^HH_WEB_PORT=/d' $ENV_FILE"
    echo "       echo 'HH_WEB_PORT=8080' | sudo tee -a $ENV_FILE"
    echo "       sudo systemctl restart breathcheck"
  else
    echo "    check: journalctl -u breathcheck -n 30 --no-pager"
  fi
fi

# 5. Take the screen now, without waiting for a reboot.
echo "==> Starting the kiosk on the device screen"
pkill chromium 2>/dev/null || true
sleep 1
if [[ -n ${DISPLAY:-} ]] || [[ -e /tmp/.X11-unix/X0 ]]; then
  DISPLAY="${DISPLAY:-:0}" nohup /bin/bash "$APP_DIR/radxa/kiosk.sh" \
    >/tmp/breathcheck-kiosk.log 2>&1 &
  echo "    launched (log: /tmp/breathcheck-kiosk.log)"
else
  echo "    no X display detected — it will start at the next desktop login"
fi

echo
echo "==> Done. BreathCheck now starts on boot and owns the screen."
echo "    undo with: bash $APP_DIR/radxa/set-default-ui.sh --undo"
