#!/usr/bin/env python3
"""Live ADC readings from the RUNNING app - alcohol and PID, with timestamps.

Unlike rawlog.py this never touches the SPI bus. It polls the backend's
/api/sensors/live endpoint, so the app keeps running and scanning normally
while you watch. Values are raw: no baseline, no drift fit, no thresholds.

Polls 4x a second by default. The board itself produces a sample roughly
every 60 ms per channel, so every poll returns a genuinely fresh reading.

    python3 livemon.py                        # watch until Ctrl-C
    python3 livemon.py --csv live.csv         # also append to a CSV
    python3 livemon.py --interval 0.1         # poll faster (default 0.25s)
    python3 livemon.py --seconds 60           # stop automatically
    python3 livemon.py --url http://10.0.0.5:8000   # watch from another machine

Columns:
    time        host clock, milliseconds
    alc_nA      AD5941 fuel cell, nanoamps
    alc_mV      the same, through the 4k transimpedance resistor
    pid_code    AD7798 PID, raw ADC codes
    pid_mV      the same, at 0.019073 mV per code
    d_alc/d_pid change since the previous printed sample
    state       analyzer state (ready / measuring / stabilizing)

A stale reading (the board has gone quiet) is printed with its age instead of
a value, so a dead stream never looks like a flat line.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request

STALE_AFTER_S = 5.0


def fetch(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live alcohol/PID ADC readings from the running app")
    parser.add_argument("--url", default="http://127.0.0.1:8000",
                        help="backend base URL (default: %(default)s)")
    parser.add_argument("--interval", type=float, default=0.25,
                        help="seconds between polls (default: %(default)s = 4/sec)")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="stop after this long (default: run until Ctrl-C)")
    parser.add_argument("--csv", default="",
                        help="also append every sample to this CSV file")
    args = parser.parse_args()

    endpoint = args.url.rstrip("/") + "/api/sensors/live"
    writer = None
    handle = None
    if args.csv:
        handle = open(args.csv, "a", newline="", encoding="utf-8")
        writer = csv.writer(handle)
        if handle.tell() == 0:
            writer.writerow(["time", "alc_nA", "alc_mV", "pid_code", "pid_mV",
                             "state", "stream_ok"])

    print(f"polling {endpoint} every {args.interval}s — Ctrl-C to stop")
    print(f"{'time':<13}{'alc_nA':>11}{'alc_mV':>9}{'d_alc':>9}"
          f"{'pid_code':>11}{'pid_mV':>9}{'d_pid':>9}  state")

    started = time.monotonic()
    prev_alc = prev_pid = None
    try:
        while True:
            try:
                data = fetch(endpoint, timeout=max(2.0, args.interval * 4))
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                print(f"{time.strftime('%H:%M:%S'):<13}backend unreachable: {exc}")
                time.sleep(args.interval)
                continue

            alcohol, cannabis = data.get("alcohol"), data.get("cannabis")

            def cell(entry, width):
                """Raw value, or the reading's age once the stream goes quiet."""
                if entry is None:
                    return f"{'--':>{width}}"
                if entry["age_s"] > STALE_AFTER_S:
                    return f"{'stale ' + str(int(entry['age_s'])) + 's':>{width}}"
                return f"{entry['adc_raw']:>{width}.1f}"

            def millivolts(entry):
                if entry is None:
                    return f"{'--':>9}"
                return f"{entry['mv']:>9.3f}"

            def step(entry, previous):
                if entry is None or previous is None:
                    return f"{'--':>9}"
                return f"{entry['adc_raw'] - previous:>+9.1f}"

            line = (f"{data.get('server_time', ''):<13}"
                    f"{cell(alcohol, 11)}"
                    f"{millivolts(alcohol)}"
                    f"{step(alcohol, prev_alc)}"
                    f"{cell(cannabis, 11)}"
                    f"{millivolts(cannabis)}"
                    f"{step(cannabis, prev_pid)}"
                    f"  {data.get('state', '')}")
            if not data.get("stream_ok", True):
                line += "  [STREAM DOWN]"
            print(line)

            if writer is not None:
                writer.writerow([
                    data.get("server_time", ""),
                    (alcohol or {}).get("adc_raw", ""),
                    (alcohol or {}).get("mv", ""),
                    (cannabis or {}).get("adc_raw", ""),
                    (cannabis or {}).get("mv", ""),
                    data.get("state", ""), data.get("stream_ok", ""),
                ])
                handle.flush()   # survive a Ctrl-C mid-test

            if alcohol is not None:
                prev_alc = alcohol["adc_raw"]
            if cannabis is not None:
                prev_pid = cannabis["adc_raw"]

            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
    finally:
        if handle is not None:
            handle.close()
            print(f"wrote {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
