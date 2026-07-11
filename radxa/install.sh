#!/usr/bin/env bash
# BreathCheck Radxa installer. Run ONCE on the device:
#
#   sudo bash radxa/install.sh
#
# It installs system + Python dependencies, registers the backend as a
# systemd service (starts on boot, restarts on crash), and sets the
# fullscreen Chromium kiosk to open when the desktop session logs in.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash radxa/install.sh" >&2
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$APP_DIR/.venv"
ENV_FILE=/etc/breathcheck.env
KIOSK_USER="${SUDO_USER:-$(id -un 1000 2>/dev/null || echo root)}"

echo "==> Installing BreathCheck from $APP_DIR"

echo "==> System packages"
apt-get update
apt-get install -y python3 python3-venv python3-pip curl
apt-get install -y chromium || apt-get install -y chromium-browser \
  || echo "WARN: no chromium package found — install a Chromium browser manually for the kiosk"
apt-get install -y unclutter || true   # hides the mouse cursor on the touchscreen

echo "==> Python environment"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" python-periphery pyserial

echo "==> Device configuration"
if [[ -f $ENV_FILE ]]; then
  echo "    $ENV_FILE already exists — keeping your settings"
else
  cp "$APP_DIR/radxa/breathcheck.env" "$ENV_FILE"
  echo "    installed $ENV_FILE"
fi

echo "==> Backend service (systemd)"
cat > /etc/systemd/system/breathcheck.service <<UNIT
[Unit]
Description=BreathCheck handheld analyzer backend
After=network.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python $APP_DIR/app.py serve
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now breathcheck.service

echo "==> Kiosk autostart for user '$KIOSK_USER'"
chmod +x "$APP_DIR/radxa/kiosk.sh" "$APP_DIR/radxa/run.sh"
KIOSK_HOME="$(getent passwd "$KIOSK_USER" | cut -d: -f6)"
AUTOSTART_DIR="$KIOSK_HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/breathcheck-kiosk.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=BreathCheck Kiosk
Exec=$APP_DIR/radxa/kiosk.sh
X-GNOME-Autostart-enabled=true
DESKTOP
chown -R "$KIOSK_USER:$KIOSK_USER" "$KIOSK_HOME/.config"

echo
echo "==> Done."
systemctl --no-pager --lines=0 status breathcheck.service || true
echo
echo "  UI       http://127.0.0.1:$(. "$ENV_FILE" 2>/dev/null; echo "${HH_WEB_PORT:-8000}")/"
echo "  Kiosk    reboot (or log out/in), or start now:  bash $APP_DIR/radxa/kiosk.sh"
echo "  Logs     journalctl -u breathcheck -f"
echo "  Config   sudo nano $ENV_FILE   then: sudo systemctl restart breathcheck"
