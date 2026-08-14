"""TEMPORARY: sensor calibration procedure.

A technician-facing flow, separate from the officer's scan screen. Delete this
module, its endpoints and the CALIBRATION button to remove the feature.

  Phase 1 - cleaning and baseline (both sensors)
    1. CLEAN     pump runs for 10 minutes to purge the sensors
    2. BASELINE  record for 1 minute once cleaning finishes
    3. VERIFY    baseline deviation must stay within tolerance
                 (100 nA for the AD5941 fuel cell, 100 mV for the AD7798 PID);
                 the mean is then saved as the zero.

  Phase 2 - span (unlocked by a valid baseline)
    SPAN     purge ethanol / target gas for 3-5 s, record a 10 s window from
             the start and integrate the curve above the baseline (mV*s).
             This integral is what correlates to PPM.
    PLATEAU  for the sensor calibrated by level rather than area: introduce
             the gas and wait for the reading to settle, then record the
             constant value at the plateau.

Both span methods record BOTH channels, since the device exposes exactly two
(AD5941 alcohol cell, AD7798 PID) and which method applies to which is the
technician's call. Raw samples for every run are written to data/calibration
as CSV so the numbers can be checked afterwards.
"""
from __future__ import annotations

import csv
import logging
import threading
import time
from typing import Any, Optional

from src import analyzer as analyzer_module
from src import config, db

logger = logging.getLogger("breathcheck.calibration")

SRC_AD5941 = analyzer_module.SRC_AD5941   # alcohol fuel cell, nA
SRC_AD7798 = analyzer_module.SRC_AD7798   # PID, ADC codes
PID_MV_PER_LSB = analyzer_module.PID_MV_PER_LSB


def _mv(source: int, raw: float) -> float:
    """Raw sample -> mV (fuel cell via Rtia, PID via LSB size)."""
    if source == SRC_AD5941:
        return raw * config.RTIA_KOHM / 1000.0     # nA -> mV
    return raw * PID_MV_PER_LSB                    # codes -> mV


def _stats(samples: list[tuple[int, float]]) -> dict[str, float]:
    values = [v for _t, v in samples]
    if not values:
        return {"n": 0, "mean": 0.0, "min": 0.0, "max": 0.0, "deviation": 0.0}
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "deviation": round(max(values) - min(values), 3),
    }


def _integral_mvs(samples: list[tuple[int, float]], baseline: float, source: int) -> float:
    """Trapezoidal integral of (sample - baseline), in mV*s."""
    total = 0.0
    previous: Optional[tuple[int, float]] = None
    for t_ms, value in samples:
        height = _mv(source, value - baseline)
        if previous is not None:
            prev_t, prev_h = previous
            total += (height + prev_h) / 2.0 * (t_ms - prev_t)
        previous = (t_ms, height)
    return round(total / 1000.0, 4)


def _find_plateau(samples: list[tuple[int, float]], source: int) -> dict[str, Any]:
    """Locate where the reading settles and report the constant value there.

    Slides a window over the trace and takes the first one whose spread stays
    within tolerance; if none qualifies, reports the flattest window found so
    the technician still gets a number plus a 'settled: false' flag.
    """
    window_ms = config.CAL_PLATEAU_WINDOW_SECONDS * 1000.0
    tolerance = (config.CAL_PLATEAU_TOLERANCE_NA if source == SRC_AD5941
                 else config.CAL_PLATEAU_TOLERANCE_MV / PID_MV_PER_LSB)
    best: Optional[tuple[float, list[float], int]] = None
    start = 0
    for end in range(len(samples)):
        while samples[end][0] - samples[start][0] > window_ms:
            start += 1
        window = [v for _t, v in samples[start:end + 1]]
        if len(window) < 5:
            continue
        spread = max(window) - min(window)
        if best is None or spread < best[0]:
            best = (spread, window, samples[start][0])
        if spread <= tolerance:
            break

    if best is None:
        return {"settled": False, "value_raw": 0.0, "value_mv": 0.0,
                "spread_raw": 0.0, "at_ms": 0}
    spread, window, at_ms = best
    mean = sum(window) / len(window)
    return {
        "settled": spread <= tolerance,
        "value_raw": round(mean, 3),
        "value_mv": round(_mv(source, mean), 4),
        "spread_raw": round(spread, 3),
        "at_ms": int(at_ms),
    }


def _save_csv(name: str, samples: dict[int, list[tuple[int, float]]]) -> str:
    """Write the raw trace of one calibration step for later analysis."""
    directory = config.DATA_DIR / "calibration"
    stamp = config.now_local().strftime("%Y%m%d_%H%M%S")
    filename = f"{stamp}_{name}.csv"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / filename).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_ms", "alcohol_na", "alcohol_mv", "pid_codes", "pid_mv"])
            alcohol = dict(samples.get(SRC_AD5941, []))
            pid = dict(samples.get(SRC_AD7798, []))
            for t_ms in sorted(set(alcohol) | set(pid)):
                a = alcohol.get(t_ms)
                p = pid.get(t_ms)
                writer.writerow([
                    t_ms,
                    "" if a is None else round(a, 3),
                    "" if a is None else round(_mv(SRC_AD5941, a), 5),
                    "" if p is None else round(p, 3),
                    "" if p is None else round(_mv(SRC_AD7798, p), 5),
                ])
    except OSError as exc:
        logger.warning("could not write calibration CSV %s: %s", filename, exc)
        return ""
    return filename


class CalibrationSession:
    """Single in-process calibration run. Only one step runs at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    # ---- state ----------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._state: dict[str, Any] = {
                "step": "idle",        # idle|clean|baseline|span|plateau
                "status": "idle",      # idle|running|done|error
                "elapsed": 0.0,
                "total": 0.0,
                "message": "Ready. Start with a 10 minute clean.",
                "error": "",
                "clean_done": False,
                "baseline": None,
                "span": None,
                "plateau": None,
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        state["can_clean"] = state["status"] != "running"
        state["can_baseline"] = state["status"] != "running" and state["clean_done"]
        baseline_ok = bool(state["baseline"] and state["baseline"].get("valid"))
        state["can_span"] = state["status"] != "running" and baseline_ok
        state["baseline_ok"] = baseline_ok
        state["clean_seconds"] = config.CAL_CLEAN_SECONDS
        state["baseline_seconds"] = config.CAL_BASELINE_SECONDS
        state["span_seconds"] = config.CAL_SPAN_SECONDS
        state["plateau_seconds"] = config.CAL_PLATEAU_MAX_SECONDS
        return state

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _busy(self) -> bool:
        with self._lock:
            return self._state["status"] == "running"

    # ---- steps ------------------------------------------------------------

    def start(self, analyzer: Any, step: str) -> tuple[bool, str]:
        """Kick off a calibration step in the background."""
        if self._busy():
            return False, "A calibration step is already running"
        if analyzer.state in ("stabilizing", "measuring"):
            return False, "Sensor is busy — try again in a moment"

        snapshot = self.snapshot()
        if step == "baseline" and not snapshot["clean_done"]:
            return False, "Run the 10 minute clean first"
        if step in ("span", "plateau") and not snapshot["baseline_ok"]:
            return False, "Record a valid baseline first"

        runners = {
            "clean": self._run_clean,
            "baseline": self._run_baseline,
            "span": self._run_span,
            "plateau": self._run_plateau,
        }
        runner = runners.get(step)
        if runner is None:
            return False, f"Unknown calibration step '{step}'"

        # Mark it running here, not in the worker: otherwise a status poll
        # issued straight after this call still sees the previous step's
        # "done" and the UI reads the run as finished before it began.
        totals = {
            "clean": config.CAL_CLEAN_SECONDS,
            "baseline": config.CAL_BASELINE_SECONDS,
            "span": config.CAL_SPAN_SECONDS,
            "plateau": config.CAL_PLATEAU_MAX_SECONDS,
        }
        self._update(step=step, status="running", elapsed=0.0,
                     total=totals[step], error="", message=f"Starting {step}...")
        threading.Thread(target=runner, args=(analyzer,), daemon=True).start()
        return True, "Started"

    def _progress(self, elapsed: float, total: float) -> None:
        self._update(elapsed=round(elapsed, 1), total=total)

    def _run(self, analyzer: Any, step: str, seconds: float, message: str,
             store: bool) -> Optional[dict[int, list[tuple[int, float]]]]:
        self._update(step=step, status="running", elapsed=0.0, total=seconds,
                     message=message, error="")
        try:
            return analyzer.collect_samples(seconds, self._progress, store=store)
        except Exception as exc:
            logger.exception("calibration step %s failed", step)
            self._update(status="error", error=str(exc),
                         message=f"{step.title()} failed: {exc}")
            return None

    def _run_clean(self, analyzer: Any) -> None:
        minutes = config.CAL_CLEAN_SECONDS / 60.0
        if self._run(analyzer, "clean", config.CAL_CLEAN_SECONDS,
                     f"Cleaning — pump running for {minutes:.0f} minutes",
                     store=False) is None:
            return
        self._update(status="done", clean_done=True,
                     message="Cleaning complete. Record the baseline next.")

    def _run_baseline(self, analyzer: Any) -> None:
        samples = self._run(analyzer, "baseline", config.CAL_BASELINE_SECONDS,
                            "Recording baseline — keep the sensor in clean air",
                            store=True)
        if samples is None:
            return

        alcohol = _stats(samples.get(SRC_AD5941, []))
        pid = _stats(samples.get(SRC_AD7798, []))
        pid_dev_mv = round(pid["deviation"] * PID_MV_PER_LSB, 4)
        alcohol_ok = alcohol["n"] > 0 and alcohol["deviation"] <= config.CAL_BASELINE_MAX_DEV_NA
        pid_ok = pid["n"] > 0 and pid_dev_mv <= config.CAL_BASELINE_MAX_DEV_MV
        valid = alcohol_ok and pid_ok

        baseline = {
            "valid": valid,
            "alcohol_ok": alcohol_ok,
            "pid_ok": pid_ok,
            "alcohol_mean_na": alcohol["mean"],
            "alcohol_deviation_na": alcohol["deviation"],
            "alcohol_limit_na": config.CAL_BASELINE_MAX_DEV_NA,
            "pid_mean_codes": pid["mean"],
            "pid_mean_mv": round(pid["mean"] * PID_MV_PER_LSB, 4),
            "pid_deviation_mv": pid_dev_mv,
            "pid_limit_mv": config.CAL_BASELINE_MAX_DEV_MV,
            "samples": alcohol["n"],
            "csv": _save_csv("baseline", samples),
        }
        if valid:
            db.set_settings({
                "cal_baseline_alcohol_na": str(alcohol["mean"]),
                "cal_baseline_pid_mv": str(baseline["pid_mean_mv"]),
                "cal_baseline_at": config.now_local().isoformat(timespec="seconds"),
            })
            message = "Baseline valid and saved. Span calibration unlocked."
        else:
            failed = []
            if not alcohol_ok:
                failed.append(f"alcohol drift {alcohol['deviation']} nA "
                              f"> {config.CAL_BASELINE_MAX_DEV_NA} nA")
            if not pid_ok:
                failed.append(f"PID drift {pid_dev_mv} mV > {config.CAL_BASELINE_MAX_DEV_MV} mV")
            message = "Baseline rejected: " + "; ".join(failed) + ". Clean again."
        self._update(status="done", baseline=baseline, message=message)

    def _run_span(self, analyzer: Any) -> None:
        samples = self._run(analyzer, "span", config.CAL_SPAN_SECONDS,
                            "Purge ethanol NOW (3-5 s) — recording", store=True)
        if samples is None:
            return

        settings = db.get_settings()
        alcohol_base = float(settings.get("cal_baseline_alcohol_na", 0) or 0)
        pid_base_mv = float(settings.get("cal_baseline_pid_mv", 0) or 0)
        pid_base_codes = pid_base_mv / PID_MV_PER_LSB if PID_MV_PER_LSB else 0.0

        alcohol_samples = samples.get(SRC_AD5941, [])
        pid_samples = samples.get(SRC_AD7798, [])
        span = {
            "alcohol_integral_mvs": _integral_mvs(alcohol_samples, alcohol_base, SRC_AD5941),
            "pid_integral_mvs": _integral_mvs(pid_samples, pid_base_codes, SRC_AD7798),
            "alcohol_peak_na": round(max((v for _t, v in alcohol_samples), default=0) - alcohol_base, 3),
            "pid_peak_mv": round((max((v for _t, v in pid_samples), default=0) - pid_base_codes)
                                 * PID_MV_PER_LSB, 4),
            "seconds": config.CAL_SPAN_SECONDS,
            "csv": _save_csv("span", samples),
        }
        db.set_settings({
            "cal_span_alcohol_mvs": str(span["alcohol_integral_mvs"]),
            "cal_span_pid_mvs": str(span["pid_integral_mvs"]),
            "cal_span_at": config.now_local().isoformat(timespec="seconds"),
        })
        self._update(status="done", span=span,
                     message=f"Span recorded — alcohol {span['alcohol_integral_mvs']} mV*s, "
                             f"PID {span['pid_integral_mvs']} mV*s. Saved.")

    def _run_plateau(self, analyzer: Any) -> None:
        samples = self._run(analyzer, "plateau", config.CAL_PLATEAU_MAX_SECONDS,
                            "Introduce the target gas — waiting for the reading to settle",
                            store=True)
        if samples is None:
            return

        alcohol = _find_plateau(samples.get(SRC_AD5941, []), SRC_AD5941)
        pid = _find_plateau(samples.get(SRC_AD7798, []), SRC_AD7798)
        plateau = {
            "alcohol_settled": alcohol["settled"],
            "alcohol_value_na": alcohol["value_raw"],
            "alcohol_value_mv": alcohol["value_mv"],
            "pid_settled": pid["settled"],
            "pid_value_codes": pid["value_raw"],
            "pid_value_mv": pid["value_mv"],
            "csv": _save_csv("plateau", samples),
        }
        db.set_settings({
            "cal_plateau_alcohol_mv": str(alcohol["value_mv"]),
            "cal_plateau_pid_mv": str(pid["value_mv"]),
            "cal_plateau_at": config.now_local().isoformat(timespec="seconds"),
        })
        settled = "settled" if (alcohol["settled"] and pid["settled"]) else "NOT fully settled"
        self._update(status="done", plateau=plateau,
                     message=f"Plateau {settled} — alcohol {alcohol['value_mv']} mV, "
                             f"PID {pid['value_mv']} mV. Saved.")


session = CalibrationSession()
