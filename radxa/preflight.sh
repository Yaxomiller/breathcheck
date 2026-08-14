#!/usr/bin/env bash
# Pre-flight check before a real calibration run.
#
#   bash radxa/preflight.sh
#
# Read-only: it changes nothing, it only reports whether this unit is in a
# state where a calibration would produce trustworthy numbers.
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE=/etc/breathcheck.env
if [[ -f $ENV_FILE ]]; then
  set -a; . "$ENV_FILE"; set +a
fi
PORT="${HH_WEB_PORT:-8000}"
BASE="http://127.0.0.1:${PORT}"

fail=0
ok()   { echo "  [ OK ] $*"; }
warn() { echo "  [WARN] $*"; }
bad()  { echo "  [FAIL] $*"; fail=$((fail + 1)); }

jqget() {  # jqget <json> <key> — no jq dependency on the device
  python3 -c "import sys,json;d=json.load(sys.stdin);v=d.get('$2');print('' if v is None else v)" <<<"$1" 2>/dev/null
}

echo "=== BreathCheck calibration pre-flight ==="
echo

echo "-- code version"
cd "$APP_DIR" && echo "  commit $(git rev-parse --short HEAD 2>/dev/null) on $(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
behind="$(git fetch --dry-run 2>&1 | head -1)"
[[ -n $behind ]] && warn "remote has newer commits — consider: git pull" || ok "up to date with the remote"
echo

echo "-- service"
state="$(systemctl is-active breathcheck 2>/dev/null)"
[[ $state == active ]] && ok "breathcheck.service is active" || bad "breathcheck.service is '$state'"

status="$(curl -fsS -m 5 "$BASE/api/status" 2>/dev/null)"
if [[ -z $status ]]; then
  bad "backend not answering at $BASE — nothing else can be checked"
  echo; echo "=== NOT READY ($fail blocking) ==="; exit 1
fi
ok "backend answering at $BASE"
echo

echo "-- sensor (the one that matters)"
analyzer="$(jqget "$status" analyzer)"
sensor_state="$(jqget "$status" sensor_state)"
stream_ok="$(jqget "$status" stream_ok)"
if [[ $analyzer == spi ]]; then
  ok "analyzer = spi — reading the real sensor board"
else
  bad "analyzer = '$analyzer' — THIS IS SIMULATED DATA, calibration would be meaningless"
  echo "         the board could not be opened; check wiring/permissions then:"
  echo "         journalctl -u breathcheck -n 40 --no-pager | grep -i 'unavailable\|spi\|gpio'"
fi
[[ $stream_ok == True || $stream_ok == true ]] && ok "frame stream is alive" \
  || bad "frame stream is down (stream_ok=$stream_ok) — the board is not sending frames"
[[ $sensor_state == ready ]] && ok "sensor state = ready" || warn "sensor state = $sensor_state (wait for 'ready')"
warnings="$(jqget "$status" warnings)"
[[ -n $warnings && $warnings != "[]" ]] && warn "startup warnings: $warnings"
echo

echo "-- calibration timings"
shortcuts=0
for key in HH_CAL_CLEAN_SECONDS HH_CAL_BASELINE_SECONDS HH_CAL_SPAN_SECONDS HH_CAL_PLATEAU_MAX_SECONDS; do
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    warn "$key is overridden in $ENV_FILE -> $(grep "^${key}=" "$ENV_FILE")"
    shortcuts=$((shortcuts + 1))
  fi
done
if [[ $shortcuts -eq 0 ]]; then
  ok "using full durations (clean 10 min, baseline 60 s, span 10 s)"
else
  warn "remove those lines for a real run: sudo sed -i '/^HH_CAL_/d' $ENV_FILE && sudo systemctl restart breathcheck"
fi
[[ -n ${HH_MOCK_SPEEDUP:-} ]] && warn "HH_MOCK_SPEEDUP=$HH_MOCK_SPEEDUP is set (mock only)" \
  || ok "no mock speedup set — timers run in real time"
echo

echo "-- calibration endpoint"
cal="$(curl -fsS -m 5 "$BASE/api/calibration" 2>/dev/null)"
if [[ -z $cal ]]; then
  bad "/api/calibration not available — the app is older than the calibration feature"
else
  step="$(jqget "$cal" step)"; cstatus="$(jqget "$cal" status)"
  ok "calibration available (step=$step status=$cstatus)"
  [[ $cstatus == running ]] && warn "a calibration step is already running"
  echo "     limits: baseline drift <= ${HH_CAL_BASELINE_MAX_DEV_NA:-100} nA (alcohol), ${HH_CAL_BASELINE_MAX_DEV_MV:-100} mV (PID)"
fi
echo

echo "-- storage"
data_dir="${HH_DATA_DIR:-$APP_DIR/data}"
mkdir -p "$data_dir/calibration" 2>/dev/null
if [[ -w $data_dir ]]; then
  ok "data dir writable: $data_dir"
  echo "     free space: $(df -h "$data_dir" | awk 'NR==2{print $4}')"
else
  bad "data dir not writable: $data_dir"
fi
echo

echo "-- hardware nodes"
[[ -e ${HH_SPI_DEVICE:-/dev/spidev1.0} ]] && ok "SPI ${HH_SPI_DEVICE:-/dev/spidev1.0} present" \
  || bad "SPI ${HH_SPI_DEVICE:-/dev/spidev1.0} missing"
[[ -e ${HH_GPIO_CHIP:-/dev/gpiochip1} ]] && ok "GPIO ${HH_GPIO_CHIP:-/dev/gpiochip1} present" \
  || bad "GPIO ${HH_GPIO_CHIP:-/dev/gpiochip1} missing"
echo

if [[ $fail -eq 0 ]]; then
  echo "=== READY for calibration ==="
  echo "    Settings -> OPEN CALIBRATION, then: CLEAN (10 min) -> BASELINE (60 s)"
  echo "    -> ETHANOL (alcohol cell) -> MYRCENE (PID)"
else
  echo "=== NOT READY — $fail blocking problem(s) above ==="
fi
exit $fail
