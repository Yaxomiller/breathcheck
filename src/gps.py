"""GPS position provider: mock (dev PC), NMEA serial module, or off."""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Optional

from src import config


class GpsProvider:
    """Reads the position on demand and caches the last good fix."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, Any] = {"fix": False, "lat": None, "lon": None, "sats": 0, "updated_at": None}

    def read(self) -> dict[str, Any]:
        if config.GPS_MODE == "mock":
            fix = self._mock_fix()
        elif config.GPS_MODE == "nmea":
            fix = self._nmea_fix()
        else:
            fix = {"fix": False, "lat": None, "lon": None, "sats": 0}
        with self._lock:
            if fix.get("fix"):
                fix["updated_at"] = time.strftime("%H:%M:%S")
                self._last = fix
            result = dict(self._last)
            result["mode"] = config.GPS_MODE
        return result

    def _mock_fix(self) -> dict[str, Any]:
        return {
            "fix": True,
            "lat": round(config.GPS_MOCK_LAT + random.uniform(-0.0004, 0.0004), 6),
            "lon": round(config.GPS_MOCK_LON + random.uniform(-0.0004, 0.0004), 6),
            "sats": random.randint(7, 12),
        }

    def _nmea_fix(self) -> dict[str, Any]:
        try:
            import serial  # pyserial, optional dependency
        except ImportError:
            return {"fix": False, "lat": None, "lon": None, "sats": 0}

        try:
            with serial.Serial(config.GPS_SERIAL_PORT, config.GPS_SERIAL_BAUD, timeout=2) as port:
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    line = port.readline().decode("ascii", errors="ignore").strip()
                    if line.startswith(("$GPGGA", "$GNGGA")):
                        fix = _parse_gga(line)
                        if fix is not None:
                            return fix
        except Exception:
            pass
        return {"fix": False, "lat": None, "lon": None, "sats": 0}


def _parse_gga(sentence: str) -> Optional[dict[str, Any]]:
    parts = sentence.split(",")
    if len(parts) < 8 or not parts[2] or not parts[4]:
        return None
    try:
        quality = int(parts[6] or 0)
        if quality == 0:
            return None
        lat = _dm_to_deg(parts[2], parts[3], degrees_len=2)
        lon = _dm_to_deg(parts[4], parts[5], degrees_len=3)
        sats = int(parts[7] or 0)
    except (ValueError, IndexError):
        return None
    return {"fix": True, "lat": round(lat, 6), "lon": round(lon, 6), "sats": sats}


def _dm_to_deg(value: str, hemisphere: str, degrees_len: int) -> float:
    degrees = float(value[:degrees_len])
    minutes = float(value[degrees_len:])
    decimal = degrees + minutes / 60.0
    if hemisphere in ("S", "W"):
        decimal = -decimal
    return decimal
