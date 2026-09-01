#!/usr/bin/env python3
"""Live raw sensor log - alcohol and PID readings with timestamps.

Prints every reading as it arrives and writes the same to CSV. Nothing is
interpreted: no baseline, no integral, no thresholds. Use it to watch the
sensors directly and mark when gas goes in and comes out.

    sudo systemctl stop breathcheck        # only one process can hold the bus
    cd ~/breathcheck
    sudo .venv/bin/python rawlog.py

While it runs:
    ENTER   mark the current moment (purge start, purge end, anything)
    Ctrl-C  stop

Marks are printed inline and saved in the CSV, so the timestamps line up with
the readings around them.

Options:
    --no-pump         leave the pump off (default: pump runs)
    --seconds N       stop automatically after N seconds
    --out FILE        write somewhere other than data/rawlog/<timestamp>.csv
    --quiet-pid       print only alcohol samples (PID still logged to CSV)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ENV_FILE = os.environ.get("HH_ENV_FILE", "/etc/breathcheck.env")
try:
    with open(ENV_FILE, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
except OSError:
    pass

from src import analyzer as analyzer_module   # noqa: E402
from src import config                        # noqa: E402

SRC_ALCOHOL = analyzer_module.SRC_AD5941
SRC_PID = analyzer_module.SRC_AD7798
NAMES = {SRC_ALCOHOL: "ALCOHOL", SRC_PID: "PID"}


def to_mv(source: int, raw: float) -> float:
    """Raw reading -> mV. Fuel cell through Rtia, PID by LSB size."""
    if source == SRC_ALCOHOL:
        return raw * config.RTIA_KOHM / 1000.0          # nA -> mV
    return raw * analyzer_module.PID_MV_PER_LSB          # codes -> mV


def main() -> int:
    parser = argparse.ArgumentParser(description="Live raw sensor log")
    parser.add_argument("--no-pump", action="store_true", help="leave the pump off")
    parser.add_argument("--seconds", type=float, default=0.0, help="stop after N seconds")
    parser.add_argument("--out", default="", help="CSV path")
    parser.add_argument("--quiet-pid", action="store_true",
                        help="print only alcohol samples (PID still logged)")
    parser.add_argument("--allow-mock", action="store_true",
                        help="run without the sensor board (values are FAKE)")
    args = parser.parse_args()

    analyzer = analyzer_module.resolve_analyzer()
    if analyzer.name != "spi" and not args.allow_mock:
        print(f"!! analyzer is '{analyzer.name}', not the sensor board.")
        for warning in analyzer.startup_warnings:
            print(f"   {warning}")
        print("\n   Readings would be SIMULATED. Usually the backend still holds")
        print("   the bus ->  sudo systemctl stop breathcheck")
        return 2

    out_path = Path(args.out) if args.out else (
        config.DATA_DIR / "rawlog" / f"{time.strftime('%Y%m%d_%H%M%S')}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 66)
    print(" Raw sensor log")
    print("=" * 66)
    print(f"sensor : {analyzer.name}")
    print(f"csv    : {out_path}")
    print(f"pump   : {'off' if args.no_pump else 'on'}")
    print("\nPress ENTER to mark a moment (gas in / gas out), Ctrl-C to stop.\n")
    print(f"  {'elapsed':>9}  {'clock':<9} {'sensor':<8} {'raw':>12} {'mV':>12}")
    print("  " + "-" * 56)

    stop = threading.Event()
    marks = {"n": 0}
    started = time.monotonic()
    handle = out_path.open("w", newline="", encoding="utf-8")
    writer = csv.writer(handle)
    writer.writerow(["elapsed_s", "clock_ist", "tick_ms", "sensor", "raw", "mv", "mark"])
    lock = threading.Lock()

    def clock() -> str:
        return config.now_local().strftime("%H:%M:%S")

    def on_sample(tick: int, source: int, value: float) -> None:
        elapsed = time.monotonic() - started
        mv = to_mv(source, value)
        with lock:
            writer.writerow([f"{elapsed:.2f}", clock(), tick, NAMES[source],
                             f"{value:.3f}", f"{mv:.5f}", ""])
            handle.flush()          # survive a Ctrl-C mid-run
            if not (args.quiet_pid and source == SRC_PID):
                print(f"  {elapsed:>8.2f}s  {clock():<9} {NAMES[source]:<8} "
                      f"{value:>12.3f} {mv:>12.5f}")

    def mark_reader() -> None:
        """ENTER marks the moment; the line lands between the readings."""
        while not stop.is_set():
            try:
                if sys.stdin.readline() == "":
                    break               # stdin closed
            except Exception:
                break
            elapsed = time.monotonic() - started
            marks["n"] += 1
            label = f"MARK {marks['n']}"
            with lock:
                writer.writerow([f"{elapsed:.2f}", clock(), "", "", "", "", label])
                handle.flush()
                print(f"  {elapsed:>8.2f}s  {clock():<9} "
                      f"*** {label} ***".ljust(40))

    threading.Thread(target=mark_reader, daemon=True).start()

    def should_stop() -> bool:
        if args.seconds and (time.monotonic() - started) >= args.seconds:
            return True
        return stop.is_set()

    try:
        analyzer.stream_samples(on_sample, should_stop, pump_on=not args.no_pump)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"\n!! sensor error: {exc}")
        return 1
    finally:
        stop.set()
        with lock:
            handle.close()
        elapsed = time.monotonic() - started
        print("\n" + "-" * 58)
        print(f"stopped after {elapsed:.0f}s, {marks['n']} mark(s)")
        print(f"csv: {out_path}")
        try:
            analyzer.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
