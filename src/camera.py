"""Server-side exhale-photo capture for the Radxa's Allwinner MIPI camera.

Chromium's getUserMedia does not work with this board's MIPI/CSI camera: the
Allwinner sunxi-vin driver exposes several /dev/videoN pipeline nodes, none of
which negotiate a stream the browser can use ("NO CAMERA" in the UI even
though the sensor is physically present). The backend grabs a single JPEG
frame itself during the scan instead.

Crucially, this sensor only delivers frames through GStreamer's patched
v4l2src with the Allwinner ISP flags `en-awisp=1 en-largemode=0` — the same
pipeline the sibling attendance project uses. Plain V4L2 (ffmpeg / OpenCV
VideoCapture) never gets a frame from it. So capture is attempted in order:

  1. gst-launch-1.0 with the Allwinner ISP pipeline (this board)
  2. gst-launch-1.0 with a plain pipeline (other GStreamer cameras)
  3. ffmpeg V4L2 grab (ordinary USB/UVC webcams)

A few warm-up frames are captured and the last is kept, so the ISP's
auto-exposure has time to converge instead of returning a black first frame.
The first device+method that yields a real JPEG is cached in-process. If
nothing works, capture fails cleanly (returns False) and the app continues
without a photo — this can only add photos, never regress the no-camera path.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from src import config

logger = logging.getLogger("breathcheck.camera")

_working: Optional[Callable[[Path], bool]] = None
_MIN_JPEG_BYTES = 2000   # rules out empty/garbage frames from a bad node


def _candidate_devices() -> list[str]:
    if config.CAMERA_DEVICE:
        return [config.CAMERA_DEVICE]
    nodes = sorted(glob.glob("/dev/video*"))
    # Try /dev/video0 first (the attendance project's default), keep the rest.
    nodes.sort(key=lambda p: (p != "/dev/video0", p))
    return nodes


def _valid_jpeg(path: Path) -> bool:
    return path.is_file() and path.stat().st_size >= _MIN_JPEG_BYTES


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            argv, capture_output=True, timeout=config.CAMERA_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)
    return result.returncode, result.stderr.decode(errors="replace")[-300:]


def _gst_capture(device: str, isp: bool, out_path: Path) -> bool:
    """Capture via gst-launch-1.0, keeping the last of several warm-up frames."""
    gst = shutil.which("gst-launch-1.0")
    if not gst:
        return False

    frames = max(1, config.CAMERA_WARMUP_FRAMES)
    quality = max(10, min(100, config.CAMERA_JPEG_QUALITY))
    tmpdir = Path(tempfile.mkdtemp(prefix="bc_cam_"))
    pattern = str(tmpdir / "f_%04d.jpg")
    try:
        if config.CAMERA_PIPELINE:
            source = config.CAMERA_PIPELINE.format(
                device=device, width=config.CAMERA_WIDTH, height=config.CAMERA_HEIGHT,
            )
            argv = [gst, "-q", "-e"] + source.split() + [
                "!", "jpegenc", f"quality={quality}", "!",
                "multifilesink", f"location={pattern}",
            ]
        elif isp:
            # en-awisp=1 en-largemode=0 enables the Allwinner ISP path on the
            # Radxa's patched v4l2src — without it the sensor never streams.
            argv = [
                gst, "-q", "-e",
                "v4l2src", f"device={device}", "en-awisp=1", "en-largemode=0",
                f"num-buffers={frames}", "!",
                f"video/x-raw,format=I420,width={config.CAMERA_WIDTH},height={config.CAMERA_HEIGHT}", "!",
                "jpegenc", f"quality={quality}", "!",
                "multifilesink", f"location={pattern}",
            ]
        else:
            argv = [
                gst, "-q", "-e",
                "v4l2src", f"device={device}", f"num-buffers={frames}", "!",
                "videoconvert", "!",
                "jpegenc", f"quality={quality}", "!",
                "multifilesink", f"location={pattern}",
            ]

        returncode, stderr = _run(argv)
        produced = sorted(glob.glob(str(tmpdir / "f_*.jpg")))
        if produced:
            last = Path(produced[-1])
            if _valid_jpeg(last):
                shutil.copyfile(last, out_path)
                return True
        logger.debug("gst capture on %s (isp=%s) produced no usable frame (rc=%s): %s",
                     device, isp, returncode, stderr)
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _ffmpeg_capture(device: str, out_path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    returncode, stderr = _run(
        [ffmpeg, "-y", "-f", "v4l2", "-i", device, "-frames:v", "1", "-q:v", "3", str(out_path)]
    )
    if returncode == 0 and _valid_jpeg(out_path):
        return True
    logger.debug("ffmpeg capture on %s produced no usable frame (rc=%s): %s",
                 device, returncode, stderr)
    return False


def _strategies() -> list[Callable[[Path], bool]]:
    strategies: list[Callable[[Path], bool]] = []
    for device in _candidate_devices():
        strategies.append(lambda out, d=device: _gst_capture(d, True, out))
        strategies.append(lambda out, d=device: _gst_capture(d, False, out))
        strategies.append(lambda out, d=device: _ffmpeg_capture(d, out))
    return strategies


def capture_jpeg(out_path: Path) -> bool:
    """Capture one JPEG frame to out_path. Returns True on success."""
    global _working

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if _working is not None:
        try:
            if _working(out_path):
                return True
        except Exception as exc:
            logger.debug("cached camera method failed: %s", exc)
        logger.warning("cached camera method stopped working, re-probing")
        _working = None

    for strategy in _strategies():
        try:
            if strategy(out_path):
                _working = strategy
                logger.info("camera capture working (%s)", getattr(strategy, "__qualname__", "strategy"))
                return True
        except Exception as exc:
            logger.debug("camera strategy raised: %s", exc)

    devices = _candidate_devices()
    tools = [t for t in ("gst-launch-1.0", "ffmpeg") if shutil.which(t)]
    logger.warning("no working camera capture found (devices=%s, tools=%s)", devices, tools or "NONE")
    return False
