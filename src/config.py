"""Environment-driven configuration for the handheld analyzer.

Every value can be overridden with an HH_* environment variable so the same
build runs on a development PC (mock sensor, mock GPS) and on the road unit
(SPI breath board, NMEA GPS) without code changes.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The unit runs in India; show and stamp everything in IST (UTC+5:30, no DST)
# regardless of the device's system timezone.
IST = timezone(timedelta(hours=5, minutes=30))


def now_local() -> datetime:
    """Current wall-clock time in IST, as a naive datetime (clean strftime)."""
    return datetime.now(IST).replace(tzinfo=None)


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


APP_NAME = "BreathCheck"
APP_VERSION = "1.1.6"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(_str("HH_DATA_DIR", str(BASE_DIR / "data")))
PHOTO_DIR = DATA_DIR / "photos"
CURVE_DIR = DATA_DIR / "curves"      # per-scan exhale ADC traces (CSV)
DB_PATH = DATA_DIR / "breathcheck.db"
FRONTEND_DIR = BASE_DIR / "frontend"

WEB_HOST = _str("HH_WEB_HOST", "0.0.0.0")
WEB_PORT = _int("HH_WEB_PORT", 8000)

# --- Breath analyzer -------------------------------------------------------
# "mock" on a PC, "spi" on the device with the sensor board attached.
ANALYZER_MODE = _str("HH_ANALYZER_MODE", "mock").lower()

# TEMPORARY (demo): force every reading's pass/fail flags to clear, so
# alcohol and cannabis both report NO no matter what the sensor measured.
# The measured values are still taken, displayed and stored — only the flags
# and the overall verdict are overridden. Set HH_DEMO_FORCE_CLEAN=0 to return
# to real pass/fail judgement.
DEMO_FORCE_CLEAN = _str("HH_DEMO_FORCE_CLEAN", "1").lower() in {"1", "true", "yes", "on"}

# Measurement cycle (seconds). The blow window itself is the officer-visible
# "scan time" setting; purge/baseline are hardware timings.
PURGE_SECONDS = _float("HH_PURGE_SECONDS", 15.0)      # pump on, sensors warming
BASELINE_SECONDS = _float("HH_BASELINE_SECONDS", 5.0)  # fresh-air zero

# STM32 SPI bridge wiring.
SPI_DEVICE = _str("HH_SPI_DEVICE", "/dev/spidev1.0")
SPI_MODE = _int("HH_SPI_MODE", 0)
SPI_SPEED_HZ = _int("HH_SPI_SPEED_HZ", 500_000)   # 500 kHz for SPI2-slave margin
GPIO_CHIP = _str("HH_GPIO_CHIP", "/dev/gpiochip1")
BOARD_ENABLE_GPIO = _int("HH_BOARD_ENABLE_GPIO", 256)   # BRD_ON, PI0 pin 26
READY_GPIO = _int("HH_READY_GPIO", 257)                 # doorbell, PI1 pin 32
PUMP_GPIO = _int("HH_PUMP_GPIO", 271)                   # air pump, PI15, ACTIVE HIGH
DOORBELL_TIMEOUT_SECONDS = _float("HH_DOORBELL_TIMEOUT_SECONDS", 5.0)
BOARD_RESET_SECONDS = _float("HH_BOARD_RESET_SECONDS", 0.1)
BOARD_BOOT_SECONDS = _float("HH_BOARD_BOOT_SECONDS", 1.0)
# Idle keepalive: after this long without a valid frame the board is reset
# and AFE sampling restarted in the background.
STREAM_DEAD_SECONDS = _float("HH_STREAM_DEAD_SECONDS", 10.0)

# Unit conversion — keep in sync with the STM32 firmware.
RTIA_KOHM = _float("HH_RTIA_KOHM", 4.0)   # AD5941 LPTIA Rtia (LPTIARTIA_4K)

# Cannabis upper/lower area ratio. The exhale trace (PID delta above the
# fresh-air baseline, in mV) is a positive bell; a horizontal line at this
# threshold splits the area under it into an upper section (the part of the
# curve poking above the line) and a lower section (the part beneath it).
# The reported ratio is upper / lower.
CANNABIS_THRESHOLD_MV = _float("HH_CANNABIS_THRESHOLD_MV", 0.4)

# Alcohol-cell stabilization at app start (fresh-air settle).
# Disabled by default: the settle wait blocked scanning for up to
# STABILIZE_MAX_S, which is too long in the field. With it off the AFE is
# still started at boot (the doorbell stream depends on it) — only the
# wait-for-drift-to-settle step is skipped, so the first reading of a cold
# session may drift more. Set HH_WARMUP_ENABLED=1 to restore it.
WARMUP_ENABLED = _str("HH_WARMUP_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
SETTLE_SLOPE_NA_S = _float("HH_SETTLE_SLOPE_NA_S", 30.0)
SETTLE_WINDOW_MS = _float("HH_SETTLE_WINDOW_MS", 10000.0)
STABILIZE_MAX_S = _float("HH_STABILIZE_MAX_S", 180.0)

# Mock reading ranges — integrals in mV*s, matching the live measurement.
MOCK_ALCOHOL_MIN = _float("HH_MOCK_ALCOHOL_MIN", 0.0)
MOCK_ALCOHOL_MAX = _float("HH_MOCK_ALCOHOL_MAX", 30.0)
MOCK_CANNABIS_MIN = _float("HH_MOCK_CANNABIS_MIN", 0.0)
MOCK_CANNABIS_MAX = _float("HH_MOCK_CANNABIS_MAX", 6.0)
# Mock calibration runs in real time by default so the countdown is honest;
# raise this only to compress long steps while working on the UI.
MOCK_SPEEDUP = _float("HH_MOCK_SPEEDUP", 1.0)

# --- Camera (server-side exhale-photo capture) ------------------------------
# Chromium's getUserMedia cannot drive this board's MIPI/CSI camera (the
# Allwinner sunxi-vin driver exposes several /dev/videoN pipeline nodes that
# never negotiate a browser-usable stream), so the exhale photo is grabbed
# server-side during the scan instead.
#
# The Allwinner sensor only delivers frames through GStreamer's patched
# v4l2src with the ISP flags `en-awisp=1 en-largemode=0` (this is what the
# sibling attendance project uses). We shell out to gst-launch-1.0 with that
# pipeline; if GStreamer/ISP isn't available we fall back to a plain ffmpeg
# grab (works for ordinary USB/UVC webcams).
#
# Leave HH_CAMERA_DEVICE blank to auto-probe /dev/video* (video0 first) and
# cache whichever node yields a real JPEG; set it (e.g. /dev/video0) to skip
# probing.
# HH_CAMERA_ENABLED=0 disables the camera completely (no preview, no exhale
# photo). The Allwinner ISP is the heaviest kernel path this app touches, so
# this is the first thing to turn off if the device becomes unstable.
CAMERA_ENABLED = _str("HH_CAMERA_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
CAMERA_DEVICE = _str("HH_CAMERA_DEVICE", "")
CAMERA_WIDTH = _int("HH_CAMERA_WIDTH", 1280)
CAMERA_HEIGHT = _int("HH_CAMERA_HEIGHT", 720)
CAMERA_WARMUP_FRAMES = _int("HH_CAMERA_WARMUP_FRAMES", 12)   # let the ISP AE settle
CAMERA_JPEG_QUALITY = _int("HH_CAMERA_JPEG_QUALITY", 85)
CAMERA_TIMEOUT_SECONDS = _float("HH_CAMERA_TIMEOUT_SECONDS", 12.0)  # ISP init is slow
CAMERA_PIPELINE = _str("HH_CAMERA_PIPELINE", "")   # full gst pipeline override

# Live preview: one persistent pipeline streams MJPEG to the browser AND
# supplies the exhale photo (grabbed from the latest frame), so the camera is
# opened once and shared. Lower resolution keeps the preview smooth.
CAMERA_STREAM_WIDTH = _int("HH_CAMERA_STREAM_WIDTH", 640)
CAMERA_STREAM_HEIGHT = _int("HH_CAMERA_STREAM_HEIGHT", 480)
CAMERA_STREAM_FPS = _int("HH_CAMERA_STREAM_FPS", 12)
CAMERA_STREAM_QUALITY = _int("HH_CAMERA_STREAM_QUALITY", 75)
# Stop the capture pipeline once no one has been watching for this long.
# Without it the pipeline keeps decoding, scaling and JPEG-encoding every
# frame for the life of the process, which is a constant CPU load on the
# handheld and makes the whole UI stutter.
CAMERA_IDLE_STOP_SECONDS = _float("HH_CAMERA_IDLE_STOP_SECONDS", 8.0)

# --- TEMPORARY: sensor calibration procedure --------------------------------
CAL_CLEAN_SECONDS = _float("HH_CAL_CLEAN_SECONDS", 600.0)        # 10 min pump purge
CAL_BASELINE_SECONDS = _float("HH_CAL_BASELINE_SECONDS", 60.0)   # 1 min baseline
CAL_SPAN_SECONDS = _float("HH_CAL_SPAN_SECONDS", 10.0)           # t0..t10 window
CAL_BASELINE_MAX_DEV_NA = _float("HH_CAL_BASELINE_MAX_DEV_NA", 100.0)  # AD5941
CAL_BASELINE_MAX_DEV_MV = _float("HH_CAL_BASELINE_MAX_DEV_MV", 100.0)  # AD7798
CAL_PLATEAU_MAX_SECONDS = _float("HH_CAL_PLATEAU_MAX_SECONDS", 120.0)
CAL_PLATEAU_WINDOW_SECONDS = _float("HH_CAL_PLATEAU_WINDOW_SECONDS", 10.0)
CAL_PLATEAU_TOLERANCE_NA = _float("HH_CAL_PLATEAU_TOLERANCE_NA", 50.0)
CAL_PLATEAU_TOLERANCE_MV = _float("HH_CAL_PLATEAU_TOLERANCE_MV", 5.0)

# --- Thermal receipt printer ------------------------------------------------
# "serial" drives an ESC/POS printer over a serial port (python-escpos);
# "mock" logs the receipt text (dev machines); "off" disables printing.
PRINTER_MODE = _str("HH_PRINTER_MODE", "mock").lower()
PRINTER_DEVICE = _str("HH_PRINTER_DEVICE", "/dev/ttyUSB0")
PRINTER_BAUD = _int("HH_PRINTER_BAUD", 9600)
PRINTER_FEED_LINES = _int("HH_PRINTER_FEED_LINES", 5)   # blank lines after the receipt
# Receipt header / device identity (printed on every receipt).
PRINT_DEVICE_NAME = _str("HH_PRINT_DEVICE_NAME", APP_NAME.upper())
PRINT_STATION_NAME = _str("HH_STATION_NAME", "")
PRINT_STATION_ADDRESS = _str("HH_STATION_ADDRESS", "")
PRINT_SERIAL_NUMBER = _str("HH_SERIAL_NUMBER", "")   # blank -> falls back to SET NO

# --- GPS --------------------------------------------------------------------
# "mock" on a PC, "nmea" with a serial GPS module, "off" to disable.
GPS_MODE = _str("HH_GPS_MODE", "mock").lower()
GPS_SERIAL_PORT = _str("HH_GPS_SERIAL_PORT", "/dev/ttyS0")
GPS_SERIAL_BAUD = _int("HH_GPS_SERIAL_BAUD", 9600)
GPS_MOCK_LAT = _float("HH_GPS_MOCK_LAT", 28.613939)
GPS_MOCK_LON = _float("HH_GPS_MOCK_LON", 77.209023)

# --- Default device settings (seeded into the DB on first boot) -------------
# units_version bumps when the meaning of the limit values changes; db.init_db
# resets stale limits so old thresholds don't silently misjudge new readings.
UNITS_VERSION = "2"
DEFAULT_ALCOHOL_LIMIT = "15"   # mV*s integral — calibrate on real hardware
DEFAULT_CANNABIS_LIMIT = "3"   # mV*s integral — calibrate on real hardware

DEFAULT_SETTINGS = {
    "area": _str("HH_AREA", "ZONE 1"),
    "version": APP_VERSION,
    "set_no": _str("HH_SET_NO", "HH-001"),
    "calibr_date": _str("HH_CALIBR_DATE", "2026-07-01"),
    "testing_mode": "ACTIVE",
    "officer": "",
    "alcohol_limit": DEFAULT_ALCOHOL_LIMIT,
    "cannabis_limit": DEFAULT_CANNABIS_LIMIT,
    "scan_seconds": "10",      # MEASURE (blow) window
    "photo_second": "4",       # seconds into the blow window
    "brightness": "80",
    "sound": "1",
    "counter": "0",
    "units_version": UNITS_VERSION,
}
