"""Environment-driven configuration for the handheld analyzer.

Every value can be overridden with an HH_* environment variable so the same
build runs on a development PC (mock sensor, mock GPS) and on the road unit
(SPI breath board, NMEA GPS) without code changes.
"""
from __future__ import annotations

import os
from pathlib import Path


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
APP_VERSION = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(_str("HH_DATA_DIR", str(BASE_DIR / "data")))
PHOTO_DIR = DATA_DIR / "photos"
DB_PATH = DATA_DIR / "breathcheck.db"
FRONTEND_DIR = BASE_DIR / "frontend"

WEB_HOST = _str("HH_WEB_HOST", "0.0.0.0")
WEB_PORT = _int("HH_WEB_PORT", 8000)

# --- Breath analyzer -------------------------------------------------------
# "mock" on a PC, "spi" on the device with the sensor board attached.
ANALYZER_MODE = _str("HH_ANALYZER_MODE", "mock").lower()

# Mock ranges. Cannabis is a raw ADC count (the app shows the raw PID value).
MOCK_ALCOHOL_MIN = _float("HH_MOCK_ALCOHOL_MIN", 0.0)      # mg/100ml BAC
MOCK_ALCOHOL_MAX = _float("HH_MOCK_ALCOHOL_MAX", 60.0)
MOCK_CANNABIS_MIN = _float("HH_MOCK_CANNABIS_MIN", 0.0)    # raw ADC counts
MOCK_CANNABIS_MAX = _float("HH_MOCK_CANNABIS_MAX", 60000.0)

# PID breath board — doorbell/frame protocol (STM32 bridge).
SPI_DEVICE = _str("HH_SPI_DEVICE", "/dev/spidev1.0")
SPI_MODE = _int("HH_SPI_MODE", 0)
SPI_SPEED_HZ = _int("HH_SPI_SPEED_HZ", 1_000_000)
GPIO_CHIP = _str("HH_GPIO_CHIP", "/dev/gpiochip1")
BOARD_ENABLE_GPIO = _int("HH_BOARD_ENABLE_GPIO", 256)   # BRD_ON, PI0 pin 26 (out)
READY_GPIO = _int("HH_READY_GPIO", 257)                 # doorbell, PI1 pin 32 (in, idle HIGH)
DOORBELL_TIMEOUT_SECONDS = _float("HH_DOORBELL_TIMEOUT_SECONDS", 5.0)

# Which record source carries the PID (cannabis) reading: 1=AD7798, 2=AD5941,
# 0=accept any source.
PID_SOURCE = _int("HH_PID_SOURCE", 1)
SAMPLE_AGGREGATION = _str("HH_SAMPLE_AGGREGATION", "mean").lower()  # mean|peak|last

# Blood alcohol: "adc" applies raw*scale+offset to the aggregated PID value,
# anything else keeps the placeholder until a real alcohol path is wired.
ALCOHOL_SOURCE = _str("HH_ALCOHOL_SOURCE", "mock").lower()  # adc|mock
ALCOHOL_SCALE = _float("HH_ALCOHOL_SCALE", 1.0)
ALCOHOL_OFFSET = _float("HH_ALCOHOL_OFFSET", 0.0)

# --- GPS --------------------------------------------------------------------
# "mock" on a PC, "nmea" with a serial GPS module, "off" to disable.
GPS_MODE = _str("HH_GPS_MODE", "mock").lower()
GPS_SERIAL_PORT = _str("HH_GPS_SERIAL_PORT", "/dev/ttyS0")
GPS_SERIAL_BAUD = _int("HH_GPS_SERIAL_BAUD", 9600)
GPS_MOCK_LAT = _float("HH_GPS_MOCK_LAT", 28.613939)
GPS_MOCK_LON = _float("HH_GPS_MOCK_LON", 77.209023)

# --- Default device settings (seeded into the DB on first boot) -------------
DEFAULT_SETTINGS = {
    "area": _str("HH_AREA", "ZONE 1"),
    "version": APP_VERSION,
    "set_no": _str("HH_SET_NO", "HH-001"),
    "calibr_date": _str("HH_CALIBR_DATE", "2026-07-01"),
    "testing_mode": "ACTIVE",
    "officer": "",
    "alcohol_limit": "30",       # mg/100ml
    "cannabis_limit": "30000",   # raw ADC counts
    "scan_seconds": "10",
    "photo_second": "4",
    "brightness": "80",
    "sound": "1",
    "counter": "0",
}
