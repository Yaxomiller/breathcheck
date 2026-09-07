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


# Same columns livemon.py prints, plus the two a breath cycle needs that a
# live view has no concept of: which phase the reading fell in, and how far it
# sat from the start of the blow.
CURVE_HEADER = ["time", "alc_nA", "alc_mV", "pid_code", "pid_mV",
                "phase", "time_ms"]


def _curve_rows(cycle: "analyzer_module.CycleResult",
                blow_seconds: float) -> list[list]:
    """Every ADC reading from both sensors, in time order, RAW.

    Values are exactly what the ADCs reported -- no baseline subtracted, no
    drift line, no thresholds -- so this file agrees with livemon.py sample
    for sample. Anything derived belongs in the result, not the trace.

    `time_ms` is measured from the start of the blow, so a 10s exhale is
    0-10000, the recovery tail runs past it, and the purge and baseline that
    came before it carry negative times.
    """
    blow_ms = max(0.0, blow_seconds) * 1000.0
    baseline_ms = max(0.0, config.BASELINE_SECONDS) * 1000.0
    rows: list[list] = []

    def phase_of(t_ms: float) -> str:
        # Negative times are the pre-blow window: the baseline the drift line
        # was fitted to, and the purge discarded before it.
        if t_ms >= blow_ms:
            return "recovery"
        if t_ms >= 0:
            return "blow"
        return "baseline" if t_ms >= -baseline_ms else "purge"

    blow_epoch = getattr(cycle, "blow_start_epoch", 0.0) or 0.0

    def stamp(t_ms: float) -> str:
        """Wall clock for one reading. time_ms is measured from the start of
        the blow, so negative offsets land correctly in the purge/baseline."""
        if not blow_epoch:
            return ""
        moment = datetime.fromtimestamp(blow_epoch + t_ms / 1000.0)
        return moment.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    # Merge both channels onto one timeline. The two ADCs sample at different
    # instants and never share a timestamp, so each row carries the other
    # channel's most recent reading forward -- the same thing livemon does
    # between polls, and the reason a row is never half empty.
    points: list[tuple[int, bool, float]] = []
    for is_alcohol, channel in ((True, cycle.alcohol), (False, cycle.cannabis)):
        for t_ms, adc, _delta, _mv in channel.samples:
            points.append((t_ms, is_alcohol, adc))
    points.sort(key=lambda point: (point[0], not point[1]))

    alc: Optional[float] = None
    pid: Optional[float] = None
    for t_ms, is_alcohol, adc in points:
        if is_alcohol:
            alc = adc
        else:
            pid = adc
        if alc is None or pid is None:
            continue   # wait for both, so no row is ever half empty
        rows.append([
            stamp(t_ms),
            round(alc, 1),
            round(analyzer_module.sample_mv(analyzer_module.SRC_AD5941, alc), 4),
            round(pid, 1),
            round(analyzer_module.sample_mv(analyzer_module.SRC_AD7798, pid), 4),
            phase_of(t_ms),
            t_ms,
        ])
    return rows


def save_curve(receipt_id: str, cycle: "analyzer_module.CycleResult",
               blow_seconds: float = 10.0) -> str:
    """Write this breath's full ADC trace to data/curves/<receipt>.csv, and
    append it to the master log. Returns the filename, or "" if empty."""
    rows = _curve_rows(cycle, blow_seconds)
    if not rows:
        return ""
    safe_name = "".join(c for c in receipt_id if c.isalnum() or c in "-_") or "curve"
    filename = f"{safe_name}.csv"
    try:
        config.CURVE_DIR.mkdir(parents=True, exist_ok=True)
        path = Path(config.CURVE_DIR) / filename
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(CURVE_HEADER)
            writer.writerows(rows)
    except OSError as exc:
        logger.warning("could not write curve CSV for %s: %s", receipt_id, exc)
        return ""
    _append_master_log(receipt_id, rows)
    return filename


def _append_master_log(receipt_id: str, rows: list[list]) -> None:
    """Append every reading to one growing CSV covering all breaths.

    data/breaths.csv is the single file to pull off the device when you want
    every sample from every test; the per-scan files stay for convenience.
    """
    try:
        path = Path(config.BREATH_LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        stamp = config.now_local().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(["receipt_id", "recorded_ist", *CURVE_HEADER])
            for row in rows:
                writer.writerow([receipt_id, stamp, *row])
    except OSError as exc:
        logger.warning("could not append to the breath log: %s", exc)


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
    # TEMPORARY (demo): report every reading as clear regardless of what was
    # measured. The raw integrals are still recorded, so the stored curve and
    # calibration data stay truthful; only the reported figures are forced.
    # Turn off with HH_DEMO_FORCE_CLEAN=0.
    if config.DEMO_FORCE_CLEAN:
        alcohol_flag = cannabis_flag = "NO"
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
        # The two figures actually shown to the officer.
        "confidence": round(config.confidence_score(areas["ratio"]), 3),
        "bac_percent": round(config.bac_percent(alcohol_value), 3),
    })
    if config.DEMO_FORCE_CLEAN:
        # Both reported figures read zero, whatever the sensors saw.
        result["bac_percent"] = 0.0
        result["confidence"] = 0.0
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
        "bac_percent": result.get("bac_percent", 0.0),
        "confidence": result.get("confidence", 0.0),
        "alcohol_flag": result["alcohol_flag"],
        "cannabis_flag": result["cannabis_flag"],
        "photo_file": "",
        "created_at": now.isoformat(timespec="seconds"),
    }
    record.update(fields)
    return record
