# BreathCheck — Police Handheld Breath Analyzer

A single-purpose kiosk app for a handheld alcohol + cannabis breath analyzer.
No login, no facial recognition — pure detection, record keeping, and GPS.

Built on the same proven stack as the attendance project it was inspired by:
FastAPI + SQLite backend, browser/kiosk frontend, SPI breath board driver
with a mock fallback for development machines.

## Screens

- **HOME** — big clock + date, four tiles: SCAN, DATABASE, GPS, SETTINGS.
- **SCAN** — 10-second exhale countdown with a live camera preview; a photo
  of the subject is captured automatically mid-exhale. Results show blood
  alcohol (mg/100ml) and cannabis (ppb) with PASS/FAIL, then a police form:
  receipt id, area, version, set no, counter, date, time, calibr date,
  gps1, gps2, name, DL number, vehicle no, test location, testing officer,
  testing mode, test result, mobile no, address, and the captured photo.
  A printable receipt is generated after saving.
- **DATABASE** — searchable list: Name, DL No, Alcohol (YES/NO),
  Cannabis (YES/NO). Tap a row for the full record + photo.
- **GPS** — live position, fix status, satellite count.
- **SETTINGS** — date & time, brightness, sound, limits (alcohol/cannabis),
  scan time, photo moment, testing mode, officer, area, set no, calibration
  date, counter, CSV export, database erase.

## Run

```bash
pip install -r requirements.txt
python app.py            # server + fullscreen kiosk browser
python app.py serve      # server only
python app.py web        # server + normal browser tab
python app.py --port 9000
```

Open `http://127.0.0.1:8000/` (camera capture requires localhost or HTTPS).

## Hardware configuration (road unit)

Everything is environment-driven; defaults are development-safe (mock sensor,
mock GPS).

```bash
# DiDies breath board via STM32 SPI bridge (requires: pip install python-periphery)
HH_ANALYZER_MODE=spi
HH_SPI_DEVICE=/dev/spidev1.0     # SPI mode 0, 500 kHz (SPI2-slave margin)
HH_GPIO_CHIP=/dev/gpiochip1
HH_BOARD_ENABLE_GPIO=256         # BRD_ON, PI0 pin 26 (opened atomically HIGH)
HH_READY_GPIO=257                # doorbell, PI1 pin 32 (idle HIGH / active LOW)
HH_PUMP_GPIO=271                 # air pump, PI15, ACTIVE HIGH
HH_RTIA_KOHM=4.0                 # AD5941 LPTIA Rtia — keep in sync with firmware
HH_PURGE_SECONDS=15              # pump-on purge before baseline
HH_BASELINE_SECONDS=5            # fresh-air zero window
HH_SETTLE_SLOPE_NA_S=30          # app-start stabilize: settled drift threshold
HH_STABILIZE_MAX_S=180

# NMEA serial GPS (requires: pip install pyserial)
HH_GPS_MODE=nmea
HH_GPS_SERIAL_PORT=/dev/ttyS0
HH_GPS_SERIAL_BAUD=9600
```

Board protocol (doorbell/frame): the board is powered once via BRD_ON
(requested atomically HIGH so the STM32 is never reset by a power blip) and
stays on between tests — the firmware's zero-offset calibration runs at its
boot. Each doorbell falling edge is answered within 100 ms by one full-duplex
246-byte transfer (header `AA 55` + record count, 20 records x 12 bytes
`uint32 tick_ms, uint16 src, 2 pad, int32 val` little-endian, CRC16-CCITT).
Commands ride on frame byte 0: `0xA0/0xA1` PID on/off, `0xB0/0xB1` alcohol
AFE on/off. Sources: 1 = AD7798 (PID/cannabis, codes at 19.073 µV/LSB),
2 = AD5941 (alcohol fuel cell, nA; V = I·Rtia at 4 kΩ), 3 = SYS 1 Hz
keepalive (bit0 PID on, bit1 AFE running).

Measurement cycle, timed in the STM32 tick domain: **15 s purge** (pump ON,
GPIO 271 active-high) → **5 s fresh-air baseline** → **10 s blow**. Per-sample
delta = value − baseline; the reported reading for BOTH channels is the
**trapezoidal integral of the delta in mV·s** (the AL-05P datasheet specs
linearity as the integral of output), with peak deltas stored alongside
(alcohol peak in µA, cannabis peak in mV). After the cycle, sensors are shut
down and the alcohol cell idles virtually shorted (biased at 0 V) — a 2-lead
fuel cell must be stored shorted or it polarizes and drifts.

At app start the backend runs a **stabilize pass** (pump off, PID off, AFE
sampling on) until the alcohol baseline drift stays under 30 nA/s, then stops
sampling with the cell still biased. The home screen shows SENSOR WARM-UP
during this; scans are rejected with a friendly message until it finishes.

**Readings are uncalibrated mV·s integrals** — the PASS/FAIL limits in
Settings are compared in mV·s and must be set after calibration. The API/DB
fields are still named `alcohol_bac` / `cannabis_ppb` for compatibility.

On Linux the brightness setting drives `/sys/class/backlight`; elsewhere the
UI applies a software dim so the control still works everywhere.

## Data

- SQLite database: `data/breathcheck.db`
- Exhale photos: `data/photos/<receipt-id>.jpg`
- CSV export: Settings → DATA → EXPORT CSV

## API

`GET /api/status`, `POST /api/scan/start`, `GET /api/scan/{id}`,
`POST /api/records`, `GET /api/records?q=`, `GET /api/records/{id}`,
`GET /api/gps`, `GET/PUT /api/settings`, `POST /api/time`,
`GET /api/export.csv`
