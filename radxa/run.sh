#!/usr/bin/env bash
# Manual run with the hardware configuration — for testing over SSH or a
# terminal without the systemd service (stop it first to free the port:
# sudo systemctl stop breathcheck). GPIO/SPI/backlight need root:
#
#   sudo bash radxa/run.sh            # server only
#   sudo bash radxa/run.sh kiosk      # server + fullscreen browser
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENV_FILE=/etc/breathcheck.env
if [[ ! -f $ENV_FILE ]]; then
  ENV_FILE="$APP_DIR/radxa/breathcheck.env"
fi
set -a; . "$ENV_FILE"; set +a

PY="$APP_DIR/.venv/bin/python"
if [[ ! -x $PY ]]; then
  PY=python3
fi

exec "$PY" "$APP_DIR/app.py" "${1:-serve}"
