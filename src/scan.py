"""Shared scan-result evaluation.

Both the web backend (src/server.py) and the terminal client
(src/terminal.py) turn a raw analyzer CycleResult into the same result dict
here, so the limit/flag/demo logic lives in exactly one place and can't drift
between the two front-ends.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src import analyzer as analyzer_module
from src import config

logger = logging.getLogger("breathcheck.scan")


def area_ratio(samples, threshold_mv: float) -> dict[str, float]:
    """Split the area under the exhale curve with a horizontal line at
    `threshold_mv` and return upper/lower areas (mV*s) plus their ratio.

    The trace is a positive bell. For each sample the curve height y is
    clamped at 0 (noise below the baseline is not negative area):
      upper contribution = max(0, y - threshold)   -> the cap above the line
      lower contribution = min(y, threshold)       -> the part beneath the line
    Both are integrated over time with the trapezoid rule, so
    upper + lower == the total area under the curve.
    """
    upper_ms = lower_ms = 0.0
    previous: Optional[tuple[int, float, float]] = None
    for sample in samples:
        t_ms, _adc, _delta, mv = sample
        height = max(0.0, float(mv))
        upper = max(0.0, height - threshold_mv)
        lower = min(height, threshold_mv)
        if previous is not None:
            prev_t, prev_upper, prev_lower = previous
            span = t_ms - prev_t
            upper_ms += (upper + prev_upper) / 2.0 * span
            lower_ms += (lower + prev_lower) / 2.0 * span
        previous = (t_ms, upper, lower)

    upper_mvs = upper_ms / 1000.0      # mV*ms -> mV*s
    lower_mvs = lower_ms / 1000.0
    ratio = (upper_mvs / lower_mvs) if lower_mvs > 0 else 0.0
    return {
        "upper": round(upper_mvs, 4),
        "lower": round(lower_mvs, 4),
        "ratio": round(ratio, 3),
        "threshold": threshold_mv,
        "points": len(samples),
    }


def save_curve(receipt_id: str, cycle: "analyzer_module.CycleResult") -> str:
    """Write the exhale ADC trace for this scan to data/curves/<receipt>.csv.
    Returns the filename, or "" if there was nothing to write."""
    samples = cycle.cannabis.samples
    if not samples:
        return ""
    safe_name = "".join(c for c in receipt_id if c.isalnum() or c in "-_") or "curve"
    filename = f"{safe_name}.csv"
    try:
        config.CURVE_DIR.mkdir(parents=True, exist_ok=True)
        path = Path(config.CURVE_DIR) / filename
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_ms", "adc_code", "delta_code", "delta_mv"])
            for t_ms, adc, delta, mv in samples:
                writer.writerow([t_ms, adc, round(delta, 3), round(mv, 5)])
    except OSError as exc:
        logger.warning("could not write curve CSV for %s: %s", receipt_id, exc)
        return ""
    return filename


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
    # Upper/lower area split of the exhale curve at the cannabis threshold.
    areas = area_ratio(cycle.cannabis.samples, config.CANNABIS_THRESHOLD_MV)
    result.update({
        "cannabis_ratio": areas["ratio"],
        "cannabis_upper": areas["upper"],
        "cannabis_lower": areas["lower"],
        "cannabis_threshold": areas["threshold"],
        "cannabis_points": areas["points"],
    })
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
        "cannabis_ratio": result.get("cannabis_ratio", 0.0),
        "cannabis_upper": result.get("cannabis_upper", 0.0),
        "cannabis_lower": result.get("cannabis_lower", 0.0),
        "alcohol_flag": result["alcohol_flag"],
        "cannabis_flag": result["cannabis_flag"],
        "photo_file": "",
        "created_at": now.isoformat(timespec="seconds"),
    }
    record.update(fields)
    return record
