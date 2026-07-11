"""Device-level controls: screen backlight and system clock.

On the Linux handheld the backlight is set through sysfs and the clock with
`date`. On other platforms these calls degrade gracefully: the brightness
value is still persisted (the UI applies a software dim) and time-set returns
a warning instead of failing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

_BACKLIGHT_ROOT = Path("/sys/class/backlight")


def _backlight_dir() -> Optional[Path]:
    if not _BACKLIGHT_ROOT.is_dir():
        return None
    for entry in sorted(_BACKLIGHT_ROOT.iterdir()):
        if (entry / "brightness").exists() and (entry / "max_brightness").exists():
            return entry
    return None


def set_backlight(percent: int) -> bool:
    """Set hardware backlight 10..100%. Returns True when hardware applied."""
    percent = max(10, min(100, int(percent)))
    device = _backlight_dir()
    if device is None:
        return False
    try:
        max_value = int((device / "max_brightness").read_text().strip())
        value = max(1, round(max_value * percent / 100))
        (device / "brightness").write_text(str(value))
        return True
    except (OSError, ValueError):
        return False


def set_system_time(iso_datetime: str) -> tuple[bool, str]:
    """Try to set the OS clock to 'YYYY-MM-DD HH:MM:SS'. Returns (ok, message)."""
    if sys.platform.startswith("linux"):
        try:
            result = subprocess.run(
                ["date", "-s", iso_datetime],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                subprocess.run(["hwclock", "-w"], capture_output=True, timeout=5)
                return True, "Clock updated"
            return False, "Needs root access on this device"
        except (OSError, subprocess.TimeoutExpired):
            return False, "Clock command unavailable"
    return False, "Clock can only be set on the handheld device"
