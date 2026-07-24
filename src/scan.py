"""Shared scan-result evaluation.

Both the web backend (src/server.py) and the terminal client
(src/terminal.py) turn a raw analyzer CycleResult into the same result dict
here, so the limit/flag/demo logic lives in exactly one place and can't drift
between the two front-ends.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from src import analyzer as analyzer_module
from src import config


def new_receipt(counter: int, now: Optional[datetime] = None) -> str:
    now = now or config.now_local()
    return f"R{now.strftime('%y%m%d')}-{counter:04d}"


def build_result(cycle: "analyzer_module.CycleResult", settings: dict,
                 now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or config.now_local()
    alcohol_limit = float(settings.get("alcohol_limit", config.DEFAULT_ALCOHOL_LIMIT))
    cannabis_limit = float(settings.get("cannabis_limit", config.DEFAULT_CANNABIS_LIMIT))
    alcohol_value = cycle.alcohol.integral_mvs
    cannabis_value = cycle.cannabis.integral_mvs
    alcohol_flag = "YES" if alcohol_value > alcohol_limit else "NO"
    cannabis_flag = "YES" if cannabis_value > cannabis_limit else "NO"
    result = {
        # Compat keys: alcohol_bac / cannabis_ppb carry the mV*s integrals of
        # the delta above the fresh-air baseline.
        "alcohol_bac": alcohol_value,
        "cannabis_ppb": cannabis_value,
        # AD5941 in uA, AD7798 in mV.
        "alcohol_baseline": round(cycle.alcohol.baseline / 1000.0, 3),
        "alcohol_peak": round(cycle.alcohol.peak / 1000.0, 3),
        "cannabis_baseline": round(cycle.cannabis.baseline * analyzer_module.PID_MV_PER_LSB, 3),
        "cannabis_peak": round(cycle.cannabis.peak * analyzer_module.PID_MV_PER_LSB, 3),
        # TEMPORARY debug fields: sensor-native units (AD5941 nA, AD7798 codes).
        "alcohol_baseline_raw": round(cycle.alcohol.baseline, 1),
        "alcohol_peak_raw": round(cycle.alcohol.peak, 1),
        "cannabis_baseline_raw": round(cycle.cannabis.baseline, 1),
        "cannabis_peak_raw": round(cycle.cannabis.peak, 1),
        "baseline_stable": cycle.alcohol.stable and cycle.cannabis.stable,
        "alcohol_flag": alcohol_flag,
        "cannabis_flag": cannabis_flag,
        "alcohol_limit": alcohol_limit,
        "cannabis_limit": cannabis_limit,
        "test_result": "FAIL" if "YES" in (alcohol_flag, cannabis_flag) else "PASS",
        "test_date": now.strftime("%Y-%m-%d"),
        "test_time": now.strftime("%H:%M:%S"),
    }
    return result


def record_from_result(result: dict, session: dict, fields: dict,
                        now: Optional[datetime] = None) -> dict[str, Any]:
    """Assemble a DB record from a completed result, the scan session
    (receipt/counter/device identity) and the officer-entered fields."""
    now = now or config.now_local()
    record = {
        "receipt_id": session["receipt_id"],
        "area": session.get("area", ""),
        "version": session.get("version", config.APP_VERSION),
        "set_no": session.get("set_no", ""),
        "counter": session.get("counter", 0),
        "test_date": result["test_date"],
        "test_time": result["test_time"],
        "calibr_date": session.get("calibr_date", ""),
        "gps1": session.get("gps1", ""),
        "gps2": session.get("gps2", ""),
        "testing_mode": session.get("testing_mode", ""),
        "test_result": result["test_result"],
        "alcohol_bac": result["alcohol_bac"],
        "cannabis_ppb": result["cannabis_ppb"],
        "alcohol_baseline": result["alcohol_baseline"],
        "alcohol_peak": result["alcohol_peak"],
        "cannabis_baseline": result["cannabis_baseline"],
        "cannabis_peak": result["cannabis_peak"],
        "alcohol_flag": result["alcohol_flag"],
        "cannabis_flag": result["cannabis_flag"],
        "photo_file": "",
        "created_at": now.isoformat(timespec="seconds"),
    }
    record.update(fields)
    return record
