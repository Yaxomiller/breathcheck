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
# PID breath board via STM32 SPI bridge (requires: pip install python-periphery)
HH_ANALYZER_MODE=spi
HH_SPI_DEVICE=/dev/spidev1.0     # SPI mode 0, 1 MHz
HH_GPIO_CHIP=/dev/gpiochip1
HH_BOARD_ENABLE_GPIO=256         # BRD_ON, PI0 pin 26 (output)
HH_READY_GPIO=257                # doorbell, PI1 pin 32 (input, idle HIGH / active LOW)
HH_PID_SOURCE=1                  # record source: 1=AD7798, 2=AD5941, 0=any
HH_SAMPLE_AGGREGATION=mean       # mean | peak | last
HH_ALCOHOL_SOURCE=mock           # alcohol stays placeholder until calibrated

# NMEA serial GPS (requires: pip install pyserial)
HH_GPS_MODE=nmea
HH_GPS_SERIAL_PORT=/dev/ttyS0
HH_GPS_SERIAL_BAUD=9600
```

Board protocol: the app powers the board via BRD_ON, waits for the doorbell
falling edge, and answers each edge with one full-duplex 246-byte transfer
(header `AA 55` + record count, 20 records x 12 bytes `uint32 tick, uint16
src, 2 pad, int32 val` little-endian, CRC16-CCITT). The first exchange sends
PID startup `0xA0`; a final exchange sends PID shutdown `0xA1`. Frames are
collected for the whole exhale window and aggregated.

**Cannabis is shown as the RAW ADC value** (no ppb conversion is applied yet):
the results card, the police form, the database detail, the CSV export and the
printed receipt all carry the raw PID reading, and the cannabis limit in
Settings is compared in raw ADC counts. The API/DB field is still named
`cannabis_ppb` for compatibility.

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
