#!/usr/bin/env python3
"""Standalone sensor calibration -> CSV.

Run it directly on the handheld. The backend must be stopped first, because
only one process can hold the SPI bus and GPIO lines:

    sudo systemctl stop breathcheck
    sudo .venv/bin/python calibrate.py

Procedure (each step waits for you to press Enter):

    1. CLEAN     pump runs 10 min to purge both sensors
    2. BASELINE  60 s in clean air; drift must stay within tolerance
                 (100 nA alcohol, 100 mV PID) and the mean becomes the zero
    3. ETHANOL   alcohol cell. A puff gives a transient, so the figure is the
                 AREA: 10 s window integrated above the baseline (mV*s)
    4. MYRCENE   PID. A sustained gas gives a steady reading, so the figure is
                 the LEVEL: the settled plateau voltage

Every step writes its raw trace (both channels, every sample) plus a summary
into data/calibration/<timestamp>/.

Useful options:
    --skip-clean            sensors already purged
    --clean-seconds 60      shorten steps while rehearsing
    --out /path/to/dir      write somewhere else
    --allow-mock            run without the sensor board (produces FAKE data)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ENV_FILE = os.environ.get("HH_ENV_FILE", "/etc/breathcheck.env")


def _load_env_file(path: str) -> bool:
    """Apply /etc/breathcheck.env the way the systemd service does.

    Run by hand the process inherits none of it, so HH_ANALYZER_MODE would
    fall back to 'mock' and the script would refuse to run on a unit that is
    wired up perfectly well. Must happen before src.config is imported, since
    that reads the environment at import time. Real environment variables win.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return True


ENV_LOADED = _load_env_file(ENV_FILE)

from src import analyzer as analyzer_module   # noqa: E402
from src import config                        # noqa: E402

SRC_ALCOHOL = analyzer_module.SRC_AD5941      # fuel cell, nA
SRC_PID = analyzer_module.SRC_AD7798          # PID, ADC codes
MV_PER_LSB = analyzer_module.PID_MV_PER_LSB


# ----------------------------------------------------------------- helpers --

def to_mv(source: int, raw: float) -> float:
    """Raw sample -> mV (fuel cell through Rtia, PID by LSB size)."""
    if source == SRC_ALCOHOL:
        return raw * config.RTIA_KOHM / 1000.0
    return raw * MV_PER_LSB


def clock(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def stats(samples: list[tuple[int, float]]) -> dict:
    values = [v for _t, v in samples]
    if not values:
        return {"n": 0, "mean": 0.0, "min": 0.0, "max": 0.0, "drift": 0.0}
    return {"n": len(values), "mean": sum(values) / len(values),
            "min": min(values), "max": max(values),
            "drift": max(values) - min(values)}


def integral_mvs(samples: list[tuple[int, float]], baseline: float, source: int) -> float:
    """Trapezoidal integral of (sample - baseline) in mV*s."""
    total, previous = 0.0, None
    for t_ms, value in samples:
        height = to_mv(source, value - baseline)
        if previous is not None:
            total += (height + previous[1]) / 2.0 * (t_ms - previous[0])
        previous = (t_ms, height)
    return total / 1000.0


def find_plateau(samples: list[tuple[int, float]], source: int,
                 window_s: float, tolerance_raw: float) -> dict:
    """First window where the reading stops moving; else the flattest one."""
    window_ms = window_s * 1000.0
    best = None
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
        if spread <= tolerance_raw:
            break
    if best is None:
        return {"settled": False, "raw": 0.0, "mv": 0.0, "spread": 0.0, "at_ms": 0}
    spread, window, at_ms = best
    mean = sum(window) / len(window)
    return {"settled": spread <= tolerance_raw, "raw": mean,
            "mv": to_mv(source, mean), "spread": spread, "at_ms": at_ms}


def write_trace(path: Path, samples: dict[int, list[tuple[int, float]]]) -> None:
    alcohol = dict(samples.get(SRC_ALCOHOL, []))
    pid = dict(samples.get(SRC_PID, []))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time_ms", "alcohol_na", "alcohol_mv", "pid_codes", "pid_mv"])
        for t_ms in sorted(set(alcohol) | set(pid)):
            a, p = alcohol.get(t_ms), pid.get(t_ms)
            writer.writerow([
                t_ms,
                "" if a is None else round(a, 3),
                "" if a is None else round(to_mv(SRC_ALCOHOL, a), 5),
                "" if p is None else round(p, 3),
                "" if p is None else round(to_mv(SRC_PID, p), 5),
            ])
    print(f"    saved {path.name}  ({sum(len(v) for v in samples.values())} samples)")


def run_step(analyzer, label: str, seconds: float, store: bool = True):
    """Sample for `seconds`, showing a live countdown."""
    print(f"\n>> {label}  ({clock(seconds)})")
    last = [-1.0]

    def progress(elapsed: float, total: float) -> None:
        if elapsed - last[0] >= 1.0 or elapsed >= total:
            last[0] = elapsed
            print(f"\r   {clock(total - elapsed)} remaining ", end="", flush=True)

    started = time.monotonic()
    samples = analyzer.collect_samples(seconds, progress, store=store)
    print(f"\r   done in {clock(time.monotonic() - started)}          ")
    return samples


def prompt(message: str) -> None:
    try:
        input(f"\n{message} [Enter] ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\nAborted.")


# -------------------------------------------------------------------- main --

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sensor calibration: clean -> baseline -> ethanol -> myrcene")
    parser.add_argument("--clean-seconds", type=float, default=config.CAL_CLEAN_SECONDS)
    parser.add_argument("--baseline-seconds", type=float, default=config.CAL_BASELINE_SECONDS)
    parser.add_argument("--span-seconds", type=float, default=config.CAL_SPAN_SECONDS)
    parser.add_argument("--plateau-seconds", type=float, default=config.CAL_PLATEAU_MAX_SECONDS)
    parser.add_argument("--skip-clean", action="store_true", help="sensors already purged")
    parser.add_argument("--allow-mock", action="store_true",
                        help="run without the sensor board (data will be FAKE)")
    parser.add_argument("--out", default="", help="output directory")
    args = parser.parse_args()

    print("=" * 60)
    print(" BreathCheck sensor calibration")
    print("=" * 60)

    print(f"\nconfig  : {ENV_FILE}" + ("" if ENV_LOADED else "  (NOT FOUND)"))
    print(f"mode    : HH_ANALYZER_MODE={config.ANALYZER_MODE}")

    analyzer = analyzer_module.resolve_analyzer()
    if analyzer.name != "spi":
        print(f"\n!! analyzer is '{analyzer.name}', not the sensor board.")
        for warning in analyzer.startup_warnings:
            print(f"   {warning}")
        if config.ANALYZER_MODE not in {"spi", "live", "hardware"}:
            # Never even tried the board.
            print(f"\n   The sensor board was not attempted: HH_ANALYZER_MODE is"
                  f" '{config.ANALYZER_MODE}'.")
            if not ENV_LOADED:
                print(f"   {ENV_FILE} was not found - point at it with"
                      f" HH_ENV_FILE=/path/to/env")
            else:
                print(f"   Set HH_ANALYZER_MODE=spi in {ENV_FILE}, or run with:")
                print("     sudo HH_ANALYZER_MODE=spi .venv/bin/python calibrate.py")
        else:
            # Tried and failed - something else holds the bus, or wiring.
            print("\n   The board could not be opened. Usually the backend still")
            print("   holds it ->  sudo systemctl stop breathcheck")
            print("   (also check nothing else drives this board, e.g."
                  " attendance-kiosk.service)")
        if not args.allow_mock:
            print("\n   Refusing to run: the numbers would be SIMULATED.")
            print("   Use --allow-mock only to rehearse the flow.")
            return 2
        print("   --allow-mock given: continuing with FAKE data.\n")

    out_dir = Path(args.out) if args.out else \
        config.DATA_DIR / "calibration" / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nsensor  : {analyzer.name}")
    print(f"output  : {out_dir}")
    print(f"limits  : alcohol drift <= {config.CAL_BASELINE_MAX_DEV_NA} nA, "
          f"PID drift <= {config.CAL_BASELINE_MAX_DEV_MV} mV")

    summary: list[tuple[str, str]] = [
        ("started_at", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("analyzer", analyzer.name),
        ("rtia_kohm", str(config.RTIA_KOHM)),
    ]

    try:
        # --- 1. clean ---------------------------------------------------
        if args.skip_clean:
            print("\n>> CLEAN skipped (--skip-clean)")
        else:
            prompt("STEP 1/4  CLEAN - pump purges both sensors. Start?")
            run_step(analyzer, "CLEAN", args.clean_seconds, store=False)
        summary.append(("clean_seconds", "skipped" if args.skip_clean else str(args.clean_seconds)))

        # --- 2. baseline ------------------------------------------------
        prompt("STEP 2/4  BASELINE - keep the sensor in CLEAN AIR. Start?")
        samples = run_step(analyzer, "BASELINE", args.baseline_seconds)
        write_trace(out_dir / "baseline.csv", samples)

        alcohol = stats(samples.get(SRC_ALCOHOL, []))
        pid = stats(samples.get(SRC_PID, []))
        pid_drift_mv = pid["drift"] * MV_PER_LSB
        pid_mean_mv = pid["mean"] * MV_PER_LSB
        alcohol_ok = alcohol["n"] > 0 and alcohol["drift"] <= config.CAL_BASELINE_MAX_DEV_NA
        pid_ok = pid["n"] > 0 and pid_drift_mv <= config.CAL_BASELINE_MAX_DEV_MV

        print(f"\n   ALCOHOL  zero {alcohol['mean']:10.2f} nA   drift {alcohol['drift']:8.2f} nA "
              f"/ {config.CAL_BASELINE_MAX_DEV_NA:.0f}   [{'OK' if alcohol_ok else 'OUT'}]")
        print(f"   PID      zero {pid_mean_mv:10.4f} mV   drift {pid_drift_mv:8.4f} mV "
              f"/ {config.CAL_BASELINE_MAX_DEV_MV:.0f}   [{'OK' if pid_ok else 'OUT'}]")
        summary += [
            ("baseline_alcohol_na", f"{alcohol['mean']:.3f}"),
            ("baseline_alcohol_drift_na", f"{alcohol['drift']:.3f}"),
            ("baseline_alcohol_ok", str(alcohol_ok)),
            ("baseline_pid_mv", f"{pid_mean_mv:.5f}"),
            ("baseline_pid_drift_mv", f"{pid_drift_mv:.5f}"),
            ("baseline_pid_ok", str(pid_ok)),
        ]
        if not (alcohol_ok and pid_ok):
            print("\n   !! baseline outside tolerance - the zero is drifting.")
            print("      Clean for longer and run again, or accept it knowingly.")
            try:
                if input("      Continue anyway? [y/N] ").strip().lower() != "y":
                    raise SystemExit("Stopped after baseline.")
            except EOFError:
                raise SystemExit("Stopped after baseline.")

        # --- 3. ethanol -> alcohol cell (area) ---------------------------
        prompt("STEP 3/4  ETHANOL - purge ethanol (3-5 s) right after you press Enter. Start?")
        samples = run_step(analyzer, "ETHANOL", args.span_seconds)
        write_trace(out_dir / "ethanol_alcohol.csv", samples)

        alcohol_samples = samples.get(SRC_ALCOHOL, [])
        pid_samples = samples.get(SRC_PID, [])
        eth_integral = integral_mvs(alcohol_samples, alcohol["mean"], SRC_ALCOHOL)
        eth_peak = max((v for _t, v in alcohol_samples), default=0.0) - alcohol["mean"]
        pid_cross = integral_mvs(pid_samples, pid["mean"], SRC_PID)
        print(f"\n   ALCOHOL  integral {eth_integral:10.4f} mV*s   peak {eth_peak:+9.2f} nA")
        print(f"   (PID during ethanol {pid_cross:.4f} mV*s - cross-check only)")
        summary += [
            ("ethanol_alcohol_integral_mvs", f"{eth_integral:.5f}"),
            ("ethanol_alcohol_peak_na", f"{eth_peak:.3f}"),
            ("ethanol_pid_integral_mvs", f"{pid_cross:.5f}"),
        ]

        # --- 4. myrcene -> PID (level) -----------------------------------
        prompt("STEP 4/4  MYRCENE - introduce the gas after you press Enter. Start?")
        samples = run_step(analyzer, "MYRCENE", args.plateau_seconds)
        write_trace(out_dir / "myrcene_pid.csv", samples)

        pid_plateau = find_plateau(
            samples.get(SRC_PID, []), SRC_PID,
            config.CAL_PLATEAU_WINDOW_SECONDS,
            config.CAL_PLATEAU_TOLERANCE_MV / MV_PER_LSB)
        alcohol_cross = find_plateau(
            samples.get(SRC_ALCOHOL, []), SRC_ALCOHOL,
            config.CAL_PLATEAU_WINDOW_SECONDS, config.CAL_PLATEAU_TOLERANCE_NA)
        print(f"\n   PID      plateau {pid_plateau['mv']:10.4f} mV   "
              f"[{'SETTLED' if pid_plateau['settled'] else 'NOT SETTLED'}] "
              f"at t+{pid_plateau['at_ms'] / 1000:.0f}s")
        print(f"   (alcohol during myrcene {alcohol_cross['mv']:.4f} mV - cross-check only)")
        summary += [
            ("myrcene_pid_plateau_mv", f"{pid_plateau['mv']:.5f}"),
            ("myrcene_pid_settled", str(pid_plateau['settled'])),
            ("myrcene_pid_at_ms", str(pid_plateau['at_ms'])),
            ("myrcene_alcohol_plateau_mv", f"{alcohol_cross['mv']:.5f}"),
        ]

    except KeyboardInterrupt:
        print("\n\nInterrupted - pump and sensors are being shut down.")
    except SystemExit as exc:
        if exc.code:
            print(f"\n{exc}")
    finally:
        summary.append(("finished_at", time.strftime("%Y-%m-%d %H:%M:%S")))
        summary_path = out_dir / "summary.csv"
        with summary_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["key", "value"])
            writer.writerows(summary)
        print(f"\nsummary -> {summary_path}")
        print(f"all files in {out_dir}")
        try:
            analyzer.shutdown()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
