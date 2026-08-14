"""FastAPI backend for the handheld analyzer.

Serves the kiosk frontend plus a small JSON API:

  GET  /api/status            device status for the home screen
  POST /api/scan/start        begin a breath test (sensor sampling in background)
  GET  /api/scan/{sid}        poll scan progress / result
  POST /api/records           save the completed test form (+ photo)
  GET  /api/records?q=        database list (name, DL, alcohol, cannabis)
  GET  /api/records/{id}      full record detail
  DELETE /api/records?confirm=ERASE   wipe database
  GET  /api/gps               current position
  GET  /api/settings          persisted device settings
  PUT  /api/settings          update settings (applies backlight)
  POST /api/time              set system clock
  GET  /api/export.csv        full CSV export
"""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import analyzer as analyzer_module
from src import calibration, camera, config, db, device, printer, scan
from src.gps import GpsProvider

app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION)
logger = logging.getLogger("breathcheck.scan")


@app.middleware("http")
async def prevent_stale_frontend(request: Request, call_next):
    """The kiosk must not retain an old scan state machine after an update."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response

_analyzer = analyzer_module.resolve_analyzer()
_gps = GpsProvider()

# Prime the alcohol cell once at app start (SPI board only): AFE sampling on,
# leaving the cell biased. With HH_WARMUP_ENABLED=1 this also waits for the
# baseline drift to settle, and scanning is blocked until it finishes.
if _analyzer.name == "spi":
    if config.WARMUP_ENABLED:
        # Set this before starting the thread so an immediate scan request
        # cannot slip through while the worker waits to be scheduled.
        _analyzer.state = "stabilizing"
        _analyzer.stabilize_started_at = time.time()
    threading.Thread(target=_analyzer.stabilize, daemon=True).start()

_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()
_MAX_SESSIONS = 50


@app.on_event("shutdown")
def _release_hardware() -> None:
    """Stop the pump when uvicorn shuts down gracefully."""
    _analyzer.shutdown()


# --- Models -----------------------------------------------------------------

class RecordIn(BaseModel):
    receipt_id: str
    area: str = ""
    version: str = ""
    set_no: str = ""
    counter: int = 0
    test_date: str = ""
    test_time: str = ""
    calibr_date: str = ""
    gps1: str = ""
    gps2: str = ""
    name: str = ""
    dl_number: str = ""
    vehicle_no: str = ""
    test_location: str = ""
    testing_officer: str = ""
    testing_mode: str = ""
    test_result: str = ""
    alcohol_bac: float = 0.0
    cannabis_ppb: float = 0.0
    alcohol_baseline: float = 0.0
    alcohol_peak: float = 0.0
    cannabis_baseline: float = 0.0
    cannabis_peak: float = 0.0
    cannabis_ratio: float = 0.0
    cannabis_upper: float = 0.0
    cannabis_lower: float = 0.0
    curve_file: str = ""
    alcohol_flag: str = "NO"
    cannabis_flag: str = "NO"
    mobile_no: str = ""
    address: str = ""
    photo_b64: str = ""


class SettingsIn(BaseModel):
    area: Optional[str] = None
    set_no: Optional[str] = None
    calibr_date: Optional[str] = None
    testing_mode: Optional[str] = None
    officer: Optional[str] = None
    alcohol_limit: Optional[float] = None
    cannabis_limit: Optional[float] = None
    scan_seconds: Optional[int] = None
    photo_second: Optional[int] = None
    brightness: Optional[int] = None
    sound: Optional[bool] = None


class TimeIn(BaseModel):
    datetime: str  # "YYYY-MM-DD HH:MM:SS"


class PrintIn(BaseModel):
    record_id: Optional[int] = None
    receipt_id: Optional[str] = None


# --- Status -------------------------------------------------------------------

@app.get("/api/status")
def status() -> dict[str, Any]:
    now = config.now_local()
    settings = db.get_settings()
    stabilize = dict(getattr(_analyzer, "last_stabilize", {}))
    stabilize_started_at = getattr(_analyzer, "stabilize_started_at", None)
    if _analyzer.state == "stabilizing" and stabilize_started_at is not None:
        stabilize.update({
            "elapsed_s": round(max(0.0, time.time() - stabilize_started_at), 1),
            "max_s": config.STABILIZE_MAX_S,
        })
    return {
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "day": now.strftime("%A"),
        "analyzer": _analyzer.name,
        "gps_mode": config.GPS_MODE,
        "records": db.count_records(),
        "counter": int(settings.get("counter", "0")),
        "set_no": settings.get("set_no", ""),
        "sensor_state": _analyzer.state,
        "stream_ok": bool(getattr(_analyzer, "stream_ok", True)),
        "printer_mode": config.PRINTER_MODE,
        "stabilize": stabilize,
        "purge_seconds": config.PURGE_SECONDS,
        "baseline_seconds": config.BASELINE_SECONDS,
        "warnings": list(_analyzer.startup_warnings),
    }


# --- Scan flow ----------------------------------------------------------------

def _capture_photo(session_id: str, receipt_id: str) -> None:
    """Runs in its own thread so the ffmpeg subprocess (can take ~1-2s)
    never blocks the SPI doorbell loop, which must answer within 100 ms."""
    safe_name = "".join(c for c in receipt_id if c.isalnum() or c in "-_") or "photo"
    target = config.PHOTO_DIR / f"{safe_name}.jpg"
    ok = camera.capture_jpeg(target)
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is not None:
            session["photo_captured"] = ok


def _run_scan(session_id: str, measure_seconds: float, receipt_id: str, photo_second: float) -> None:
    photo_state = {"triggered": False}

    def progress(phase: str, elapsed: float, total: float) -> None:
        with _sessions_lock:
            session = _sessions.get(session_id)
            if session is not None and session["status"] == "running":
                session["phase"] = phase
                session["phase_elapsed"] = round(elapsed, 2)
                session["phase_total"] = total
                session["phase_at"] = time.time()
        if phase == "measure" and not photo_state["triggered"] and elapsed >= photo_second:
            photo_state["triggered"] = True
            threading.Thread(target=_capture_photo, args=(session_id, receipt_id), daemon=True).start()

    try:
        cycle = _analyzer.run_cycle(measure_seconds, progress)
        settings = db.get_settings()
        result = scan.build_result(cycle, settings)
        # Persist the exhale ADC trace next to the record.
        result["curve_file"] = scan.save_curve(receipt_id, cycle)
        # Log every reading straight away, so the database holds an entry for
        # each test even if the officer never fills in the subject's details.
        # Saving the form later amends this same row (upsert on receipt_id).
        with _sessions_lock:
            session = dict(_sessions.get(session_id) or {})
        try:
            db.insert_record(scan.record_from_result(result, session, {}))
        except Exception:
            logger.exception("could not log scan %s to the database", receipt_id)
        update = {"status": "done", "result": result}
    except Exception as exc:
        logger.exception("Scan %s failed", session_id)
        update = {"status": "error", "error": str(exc)}
    with _sessions_lock:
        if session_id in _sessions:
            _sessions[session_id].update(update)


@app.post("/api/scan/start")
def scan_start() -> dict[str, Any]:
    if _analyzer.state == "stabilizing":
        raise HTTPException(status_code=409, detail="SENSOR WARMING UP — TRY AGAIN SOON")
    if _analyzer.state == "measuring":
        raise HTTPException(status_code=409, detail="TEST ALREADY RUNNING")
    if not getattr(_analyzer, "stream_ok", True):
        raise HTTPException(status_code=409, detail="NO SIGNAL FROM SENSOR — RECONNECTING, TRY AGAIN SHORTLY")

    settings = db.get_settings()
    seconds = max(3, int(float(settings.get("scan_seconds", "10"))))
    counter = db.next_counter()
    now = config.now_local()
    receipt_id = f"R{now.strftime('%y%m%d')}-{counter:04d}"
    session_id = uuid.uuid4().hex[:12]

    photo_second = max(1, int(float(settings.get("photo_second", "4"))))

    fix = _gps.read()
    session = {
        "status": "running",
        "receipt_id": receipt_id,
        "counter": counter,
        "seconds": seconds,
        "phase": "starting",
        "phase_elapsed": 0.0,
        "phase_total": 0.0,
        "phase_at": time.time(),
        "photo_captured": False,
        "started_at": now.isoformat(timespec="seconds"),
        # Device identity, kept so the auto-logged record is complete even
        # before the officer fills in the subject's details.
        "area": settings.get("area", ""),
        "version": settings.get("version", config.APP_VERSION),
        "set_no": settings.get("set_no", ""),
        "calibr_date": settings.get("calibr_date", ""),
        "testing_mode": settings.get("testing_mode", "ACTIVE"),
        "gps1": str(fix["lat"]) if fix.get("fix") else "",
        "gps2": str(fix["lon"]) if fix.get("fix") else "",
    }
    with _sessions_lock:
        if len(_sessions) > _MAX_SESSIONS:
            done = [k for k, s in _sessions.items() if s["status"] != "running"]
            for key in done[:-10]:
                _sessions.pop(key, None)
        _sessions[session_id] = session

    threading.Thread(
        target=_run_scan, args=(session_id, seconds, receipt_id, photo_second), daemon=True,
    ).start()
    return {
        "session_id": session_id,
        "receipt_id": receipt_id,
        "counter": counter,
        "seconds": seconds,
        "purge_seconds": config.PURGE_SECONDS,
        "baseline_seconds": config.BASELINE_SECONDS,
        "photo_second": photo_second,
        "area": settings.get("area", ""),
        "version": settings.get("version", config.APP_VERSION),
        "set_no": settings.get("set_no", ""),
        "calibr_date": settings.get("calibr_date", ""),
        "testing_mode": settings.get("testing_mode", "ACTIVE"),
        "officer": settings.get("officer", ""),
    }


@app.get("/api/scan/{session_id}")
def scan_status(session_id: str) -> dict[str, Any]:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Unknown scan session")
        result = dict(session)
    if result["status"] == "running" and "phase_at" in result:
        drift = time.time() - result["phase_at"]
        elapsed = min(result["phase_total"], result["phase_elapsed"] + drift)
        result["phase_remaining"] = round(max(0.0, result["phase_total"] - elapsed), 2)
    return result


# --- Records --------------------------------------------------------------------

@app.post("/api/records")
def save_record(record: RecordIn) -> dict[str, Any]:
    safe_name = "".join(c for c in record.receipt_id if c.isalnum() or c in "-_") or "photo"
    photo_file = ""
    if record.photo_b64:
        try:
            payload = record.photo_b64.split(",", 1)[-1]
            raw = base64.b64decode(payload)
            photo_file = f"{safe_name}.jpg"
            (config.PHOTO_DIR / photo_file).write_bytes(raw)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=f"Photo could not be saved: {exc}")
    else:
        # No browser upload (getUserMedia unavailable on this camera) — use
        # whatever the backend already captured server-side during the scan.
        candidate = config.PHOTO_DIR / f"{safe_name}.jpg"
        if candidate.is_file():
            photo_file = candidate.name

    data = record.model_dump(exclude={"photo_b64"})
    data["photo_file"] = photo_file
    data["created_at"] = config.now_local().isoformat(timespec="seconds")
    try:
        record_id = db.insert_record(data)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Record could not be stored: {exc}")
    return {"id": record_id, "receipt_id": record.receipt_id}


@app.get("/api/records")
def records(q: str = "") -> dict[str, Any]:
    return {"records": db.list_records(q.strip())}


@app.get("/api/records/{record_id}")
def record_detail(record_id: int) -> dict[str, Any]:
    record = db.get_record(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    if record.get("photo_file"):
        record["photo_url"] = f"/photos/{record['photo_file']}"
    return record


@app.post("/api/print")
def print_receipt(body: PrintIn) -> dict[str, Any]:
    """Print a saved test on the ESC/POS serial thermal printer."""
    record = None
    if body.record_id is not None:
        record = db.get_record(body.record_id)
    elif body.receipt_id:
        record = db.get_record_by_receipt(body.receipt_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    ok, message = printer.print_record(record, db.get_settings())
    if not ok:
        raise HTTPException(status_code=503, detail=message)
    return {"ok": True, "message": message}


@app.delete("/api/records")
def wipe_records(confirm: str = "") -> dict[str, Any]:
    if confirm != "ERASE":
        raise HTTPException(status_code=400, detail="Pass confirm=ERASE to wipe the database")
    return {"deleted": db.clear_records()}


@app.get("/curves/{filename}")
def curve(filename: str) -> FileResponse:
    """The raw exhale ADC trace recorded for a scan."""
    safe_name = "".join(c for c in filename if c.isalnum() or c in "-_.")
    path = config.CURVE_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Curve not found")
    return FileResponse(path, media_type="text/csv", filename=safe_name)


@app.get("/photos/{filename}")
def photo(filename: str) -> FileResponse:
    safe_name = "".join(c for c in filename if c.isalnum() or c in "-_.")
    path = config.PHOTO_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Photo not found")
    return FileResponse(path, media_type="image/jpeg")


# --- Camera live preview -----------------------------------------------------------

@app.get("/api/camera/stream")
async def camera_stream() -> StreamingResponse:
    # Probing/first start opens the ISP (~1-2s); do it off the event loop.
    # acquire() also registers this viewer, so the pipeline is shut down once
    # the last preview goes away instead of encoding frames forever.
    started = await run_in_threadpool(camera.streamer.acquire)
    if not started:
        raise HTTPException(status_code=503, detail="Camera unavailable")

    async def frames():
        idle = 0
        last = None
        try:
            while True:
                frame = camera.streamer.latest_jpeg()
                if frame is not None and frame is not last:
                    last = frame
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                           b"Cache-Control: no-store, no-cache, must-revalidate\r\n\r\n"
                           + frame + b"\r\n")
                    idle = 0
                else:
                    idle += 1
                    if idle > 250:   # ~15s with no new frame — let the <img> retry
                        break
                await asyncio.sleep(1.0 / max(1, config.CAMERA_STREAM_FPS))
        finally:
            # Runs on client disconnect too, so navigating away releases it.
            camera.streamer.release()

    return StreamingResponse(
        frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# --- GPS --------------------------------------------------------------------------

@app.get("/api/gps")
def gps() -> dict[str, Any]:
    return _gps.read()


# --- Settings ----------------------------------------------------------------------

@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    settings = db.get_settings()
    return {
        "area": settings.get("area", ""),
        "version": settings.get("version", config.APP_VERSION),
        "set_no": settings.get("set_no", ""),
        "calibr_date": settings.get("calibr_date", ""),
        "testing_mode": settings.get("testing_mode", "ACTIVE"),
        "officer": settings.get("officer", ""),
        "alcohol_limit": float(settings.get("alcohol_limit", "30")),
        "cannabis_limit": float(settings.get("cannabis_limit", "10")),
        "scan_seconds": int(float(settings.get("scan_seconds", "10"))),
        "photo_second": int(float(settings.get("photo_second", "4"))),
        "brightness": int(float(settings.get("brightness", "80"))),
        "sound": settings.get("sound", "1") == "1",
        "counter": int(settings.get("counter", "0")),
        "analyzer": _analyzer.name,
        "gps_mode": config.GPS_MODE,
    }


@app.put("/api/settings")
def put_settings(update: SettingsIn) -> dict[str, Any]:
    values: dict[str, str] = {}
    hardware_backlight = None
    for key, value in update.model_dump(exclude_none=True).items():
        if key == "sound":
            values[key] = "1" if value else "0"
        elif key == "brightness":
            percent = max(10, min(100, int(value)))
            values[key] = str(percent)
            hardware_backlight = device.set_backlight(percent)
        elif key == "testing_mode":
            values[key] = "PASSIVE" if str(value).upper() == "PASSIVE" else "ACTIVE"
        elif key in {"alcohol_limit", "cannabis_limit"}:
            values[key] = str(max(0.0, float(value)))
        elif key in {"scan_seconds", "photo_second"}:
            values[key] = str(max(1, min(60, int(value))))
        else:
            values[key] = str(value).strip()
    if values:
        db.set_settings(values)
    result = get_settings()
    if hardware_backlight is not None:
        result["hardware_backlight"] = hardware_backlight
    return result


# --- TEMPORARY: calibration procedure ------------------------------------------------

@app.get("/api/calibration")
def calibration_status() -> dict[str, Any]:
    return calibration.session.snapshot()


@app.post("/api/calibration/{step}")
def calibration_start(step: str) -> dict[str, Any]:
    if step == "reset":
        calibration.session.reset()
        return calibration.session.snapshot()
    if _analyzer.name != "spi":
        # Mock analyzers can still run the flow (compressed timings) for UI work.
        logger.info("calibration '%s' running against the %s analyzer", step, _analyzer.name)
    ok, message = calibration.session.start(_analyzer, step)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return calibration.session.snapshot()


@app.post("/api/time")
def set_time(body: TimeIn) -> dict[str, Any]:
    ok, message = device.set_system_time(body.datetime.strip())
    return {"ok": ok, "message": message}


# --- Export -------------------------------------------------------------------------

@app.get("/api/export.csv")
def export_csv() -> StreamingResponse:
    rows = db.all_records()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    header = ["id", *db.RECORD_FIELDS]
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(column, "") for column in header])
    buffer.seek(0)
    filename = f"breathcheck_{config.now_local().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# --- Frontend (must be mounted last) ---------------------------------------------------

app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
