"""Server-side camera for the Radxa's Allwinner MIPI sensor.

Chromium's getUserMedia cannot drive this board's MIPI/CSI camera: the
Allwinner sunxi-vin driver only delivers frames through GStreamer's patched
v4l2src with the ISP flags `en-awisp=1 en-largemode=0` (the same pipeline the
sibling attendance project uses). Plain V4L2 (ffmpeg / OpenCV / browser) never
gets a frame from it.

So the backend owns the camera. Two things use it, both from ONE shared
pipeline:

  * live preview  — a persistent gst pipeline encodes MJPEG to stdout; the
    reader thread keeps the latest frame; the web app shows it as an <img>
    pointed at /api/camera/stream (an MJPEG multipart response).
  * exhale photo  — grabbed from that same latest frame during the blow.

If the streamer isn't running (e.g. the terminal client), a one-shot grab is
used instead. Capture order for probing: gst ISP pipeline, gst plain, then
ffmpeg (ordinary USB/UVC webcams). Everything fails cleanly to "no photo /
no preview" when no camera is present — this never blocks a scan.
"""
from __future__ import annotations

import glob
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from src import config

logger = logging.getLogger("breathcheck.camera")

_MIN_JPEG_BYTES = 2000            # rules out empty/garbage frames from a bad node
_SOI = b"\xff\xd8"               # JPEG start-of-image
_EOI = b"\xff\xd9"               # JPEG end-of-image

# Cached probe result: (mode, device) where mode is gst-isp | gst-plain | ffmpeg
_probe_lock = threading.Lock()
_probed = False
_probe_result: Optional[tuple[str, str]] = None


def _candidate_devices() -> list[str]:
    if not config.CAMERA_ENABLED:
        return []          # HH_CAMERA_ENABLED=0: never touch the camera at all
    if config.CAMERA_DEVICE:
        return [config.CAMERA_DEVICE]
    nodes = sorted(glob.glob("/dev/video*"))
    nodes.sort(key=lambda p: (p != "/dev/video0", p))   # try /dev/video0 first
    return nodes


def _valid_jpeg(path: Path) -> bool:
    return path.is_file() and path.stat().st_size >= _MIN_JPEG_BYTES


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, capture_output=True, timeout=config.CAMERA_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)
    return result.returncode, result.stderr.decode(errors="replace")[-300:]


# --- one-shot grab (probing + terminal / no-preview path) -------------------

def _gst_oneshot(device: str, isp: bool, out_path: Path) -> bool:
    gst = shutil.which("gst-launch-1.0")
    if not gst:
        return False
    frames = max(1, config.CAMERA_WARMUP_FRAMES)
    quality = max(10, min(100, config.CAMERA_JPEG_QUALITY))
    tmpdir = Path(tempfile.mkdtemp(prefix="bc_cam_"))
    pattern = str(tmpdir / "f_%04d.jpg")
    try:
        if isp:
            argv = [gst, "-q", "-e", "v4l2src", f"device={device}",
                    "en-awisp=1", "en-largemode=0", f"num-buffers={frames}", "!",
                    f"video/x-raw,format=I420,width={config.CAMERA_WIDTH},height={config.CAMERA_HEIGHT}",
                    "!", "jpegenc", f"quality={quality}", "!", "multifilesink", f"location={pattern}"]
        else:
            argv = [gst, "-q", "-e", "v4l2src", f"device={device}", f"num-buffers={frames}", "!",
                    "videoconvert", "!", "jpegenc", f"quality={quality}", "!",
                    "multifilesink", f"location={pattern}"]
        returncode, stderr = _run(argv)
        produced = sorted(glob.glob(str(tmpdir / "f_*.jpg")))
        if produced and _valid_jpeg(Path(produced[-1])):
            shutil.copyfile(produced[-1], out_path)
            return True
        logger.debug("gst oneshot %s isp=%s: rc=%s %s", device, isp, returncode, stderr)
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _ffmpeg_oneshot(device: str, out_path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    returncode, stderr = _run(
        [ffmpeg, "-y", "-f", "v4l2", "-i", device, "-frames:v", "1", "-q:v", "3", str(out_path)])
    if returncode == 0 and _valid_jpeg(out_path):
        return True
    logger.debug("ffmpeg oneshot %s: rc=%s %s", device, returncode, stderr)
    return False


def _grab(mode: str, device: str, out_path: Path) -> bool:
    if mode == "ffmpeg":
        return _ffmpeg_oneshot(device, out_path)
    return _gst_oneshot(device, mode == "gst-isp", out_path)


def _probe() -> Optional[tuple[str, str]]:
    """Find a working (mode, device) once and cache it."""
    global _probed, _probe_result
    with _probe_lock:
        if _probed:
            return _probe_result

        scratch = Path(tempfile.gettempdir()) / "bc_cam_probe.jpg"
        found: Optional[tuple[str, str]] = None
        for device in _candidate_devices():
            for mode in ("gst-isp", "gst-plain", "ffmpeg"):
                if _grab(mode, device, scratch):
                    found = (mode, device)
                    break
            if found:
                logger.info("camera found: mode=%s device=%s", *found)
                break
        try:
            scratch.unlink()
        except OSError:
            pass

        _probe_result = found
        _probed = True
        if found is None:
            tools = [t for t in ("gst-launch-1.0", "ffmpeg") if shutil.which(t)]
            logger.warning("no camera found (devices=%s tools=%s)",
                           _candidate_devices(), tools or "NONE")
        return _probe_result


# --- live MJPEG streamer (preview + shared photo source) --------------------

class CameraStreamer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._latest: Optional[bytes] = None
        self._stop = threading.Event()

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    # NOTE: the pipeline deliberately runs for the life of the process once
    # started. An earlier version stopped it when no preview was attached and
    # restarted it on demand, to save CPU -- but repeatedly tearing the
    # Allwinner ISP down and bringing it back destabilised the SoC and the
    # device crashed. Do not reintroduce that without proving it is safe.

    def _stream_argv(self, mode: str, device: str) -> Optional[list[str]]:
        capture_w, capture_h = config.CAMERA_WIDTH, config.CAMERA_HEIGHT
        stream_w, stream_h = config.CAMERA_STREAM_WIDTH, config.CAMERA_STREAM_HEIGHT
        fps = max(1, config.CAMERA_STREAM_FPS)
        q = max(10, min(100, config.CAMERA_STREAM_QUALITY))
        gst = shutil.which("gst-launch-1.0")
        if mode in ("gst-isp", "gst-plain") and gst:
            src = ["v4l2src", f"device={device}"]
            if mode == "gst-isp":
                src += ["en-awisp=1", "en-largemode=0"]
            # Open the sensor at the same native mode that probing proved works.
            # The Allwinner ISP does not negotiate the 640x480 preview size
            # directly, so any preview downscaling must happen downstream.
            caps = (
                f"video/x-raw,format=I420,width={capture_w},height={capture_h}"
                if mode == "gst-isp"
                else f"video/x-raw,width={capture_w},height={capture_h}"
            )
            convert = [] if mode == "gst-isp" else ["videoconvert", "!"]
            scale = []
            if (stream_w, stream_h) != (capture_w, capture_h):
                scale = [
                    "videoscale", "!",
                    f"video/x-raw,width={stream_w},height={stream_h}", "!",
                ]
            return (
                [gst, "-q"]
                + src
                + ["!", caps, "!", "videorate", "drop-only=true", f"max-rate={fps}", "!"]
                + scale
                + convert
                + ["videoflip", "method=rotate-180", "!",
                   "jpegenc", f"quality={q}", "!", "fdsink", "fd=1"]
            )
        ffmpeg = shutil.which("ffmpeg")
        if mode == "ffmpeg" and ffmpeg:
            return [ffmpeg, "-nostdin", "-loglevel", "error", "-f", "v4l2",
                    "-video_size", f"{stream_w}x{stream_h}", "-i", device, "-r", str(fps),
                    "-vf", "hflip,vflip", "-f", "image2pipe",
                    "-c:v", "mjpeg", "-q:v", "5", "-"]
        return None

    def _wait_for_first_frame(self, proc: subprocess.Popen) -> bool:
        """Confirm the capture pipeline is producing JPEGs before returning."""
        deadline = time.monotonic() + config.CAMERA_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            with self._lock:
                if self._proc is not proc:
                    return False
                if self._latest is not None:
                    return True
            if proc.poll() is not None:
                break
            time.sleep(0.05)

        with self._lock:
            if self._proc is proc:
                self._proc = None
                should_terminate = True
            else:
                should_terminate = False
        if should_terminate:
            try:
                proc.terminate()
            except OSError:
                pass
        logger.warning("camera stream did not produce a frame before timeout")
        return False

    def ensure_started(self) -> bool:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                proc = self._proc
                if self._latest is not None:
                    return True
            else:
                self._proc = None
                proc = None
        if proc is not None:
            return self._wait_for_first_frame(proc)

        probed = _probe()
        if probed is None:
            return False
        argv = self._stream_argv(*probed)
        if argv is None:
            return False

        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                proc = self._proc
            else:
                self._stop.clear()
                self._latest = None
                try:
                    proc = subprocess.Popen(
                        argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                except OSError as exc:
                    logger.warning("could not start camera stream: %s", exc)
                    self._proc = None
                    return False
                self._proc = proc
                self._reader = threading.Thread(target=self._read_loop, args=(proc,), daemon=True)
                self._reader.start()
                logger.info("camera stream started (%s %s)", probed[0], probed[1])
        return self._wait_for_first_frame(proc)

    def _read_loop(self, proc: subprocess.Popen) -> None:
        buffer = b""
        stdout = proc.stdout
        try:
            while not self._stop.is_set():
                chunk = stdout.read(8192) if stdout else b""
                if not chunk:
                    break
                buffer += chunk
                # Extract every complete JPEG (SOI..EOI); keep the last.
                while True:
                    start = buffer.find(_SOI)
                    if start < 0:
                        break
                    end = buffer.find(_EOI, start + 2)
                    if end < 0:
                        if start > 0:
                            buffer = buffer[start:]   # drop leading garbage
                        if len(buffer) > 4_000_000:   # runaway guard
                            buffer = b""
                        break
                    frame = buffer[start:end + 2]
                    buffer = buffer[end + 2:]
                    if len(frame) >= _MIN_JPEG_BYTES:
                        with self._lock:
                            self._latest = frame
        except Exception as exc:
            logger.debug("camera read loop ended: %s", exc)
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

    def latest_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._latest

    def save_latest(self, out_path: Path) -> bool:
        frame = self.latest_jpeg()
        if frame is None or len(frame) < _MIN_JPEG_BYTES:
            return False
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(frame)
            return True
        except OSError as exc:
            logger.warning("could not save camera frame: %s", exc)
            return False

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


streamer = CameraStreamer()


# --- public capture entry point ---------------------------------------------

def capture_jpeg(out_path: Path) -> bool:
    """Capture one JPEG to out_path. Uses the live stream's latest frame when
    the streamer is running (camera is busy), else a one-shot grab."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if streamer.is_running():
        return streamer.save_latest(out_path)
    probed = _probe()
    if probed is None:
        return False
    return _grab(probed[0], probed[1], out_path)
