#!/usr/bin/env bash
# Run BreathCheck as a text UI over SSH (no browser/kiosk).
#
#   bash radxa/term.sh
#
# The background service holds the GPIO/SPI, so this stops it first (only one
# process can own the sensor pins). Needs root for GPIO/SPI access.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENV_FILE=/etc/breathcheck.env
if [[ ! -f $ENV_FILE ]]; then
  ENV_FILE="$APP_DIR/radxa/breathcheck.env"
fi
set -a; . "$ENV_FILE"; set +a

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet breathcheck; then
  echo "Stopping breathcheck service to free the GPIO/SPI..."
  sudo systemctl stop breathcheck
fi

PY="$APP_DIR/.venv/bin/python"
if [[ ! -x $PY ]]; then
  PY=python3
fi

# sudo -E keeps the HH_* environment for the child process.
exec sudo -E "$PY" "$APP_DIR/app.py" term
