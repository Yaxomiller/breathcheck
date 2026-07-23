"""Server-side exhale-photo capture via ffmpeg + V4L2.

Chromium's getUserMedia does not work with this board's MIPI/CSI camera: the
Allwinner sunxi-vin driver exposes several /dev/videoN pipeline nodes, none
of which negotiate a stream the browser can use ("NO CAMERA" in the UI even
though the sensor is physically present). The backend grabs a single JPEG
frame straight from the video device with ffmpeg instead, during the scan —
no browser camera permission involved at all.

If HH_CAMERA_DEVICE is not set, every /dev/video* node is tried once; the
first one that yields a real JPEG is cached in-process so later captures are
a single ffmpeg call, not a fresh probe. If probing finds nothing, capture
fails cleanly (returns False) and the app continues without a photo, same as
today — this can only add photos, never regress the no-camera path.
"""
from __future__ import annotations

import glob
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from src import config

logger = logging.getLogger("breathcheck.camera")

_working_device: Optional[str] = None
_MIN_JPEG_BYTES = 2000   # rules out empty/garbage frames from a bad node


def _ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")


def _try_capture(device: str, out_path: Path) -> bool:
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        logger.warning("ffmpeg not found — cannot capture from %s", device)
        return False
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-f", "v4l2", "-i", device,
             "-frames:v", "1", "-q:v", "3", str(out_path)],
            capture_output=True, timeout=config.CAMERA_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("camera capture on %s failed: %s", device, exc)
        return False
    if result.returncode != 0 or not out_path.is_file() or out_path.stat().st_size < _MIN_JPEG_BYTES:
        logger.debug("camera capture on %s produced no usable frame (rc=%s): %s",
                      device, result.returncode, result.stderr.decode(errors="replace")[-300:])
        return False
    return True


def _candidate_devices() -> list[str]:
    if config.CAMERA_DEVICE:
        return [config.CAMERA_DEVICE]
    return sorted(glob.glob("/dev/video*"))


def capture_jpeg(out_path: Path) -> bool:
    """Capture one JPEG frame to out_path. Returns True on success."""
    global _working_device

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if _working_device is not None:
        if _try_capture(_working_device, out_path):
            return True
        logger.warning("cached camera device %s stopped working, re-probing", _working_device)
        _working_device = None

    for device in _candidate_devices():
        if _try_capture(device, out_path):
            _working_device = device
            logger.info("camera capture working on %s", device)
            return True

    logger.warning("no working camera device found among %s", _candidate_devices())
    return False
