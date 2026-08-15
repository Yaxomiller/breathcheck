#!/usr/bin/env python3
"""Print a sample receipt, to check the printer without running a scan.

    sudo .venv/bin/python radxa/print-test.py           # full sample receipt
    sudo .venv/bin/python radxa/print-test.py --line    # one line of text only

Loads /etc/breathcheck.env the way the service does, and turns on TX logging
so every byte sent to the printer is shown. Works while the app is running -
the printer is only opened for the duration of a print.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

logging.basicConfig(level=logging.INFO, format="%(message)s")

from src import config, printer   # noqa: E402


def sample_record() -> dict:
    now = config.now_local()
    return {
        "receipt_id": "SAMPLE-0001",
        "version": config.APP_VERSION,
        "set_no": "A8080T5130",
        "counter": 4965,
        "test_date": now.strftime("%Y/%m/%d"),
        "test_time": now.strftime("%H:%M:%S"),
        "calibr_date": "2026/05/16",
        "gps1": "NA", "gps2": "NA",
        "name": "SAMPLE PRINT", "dl_number": "", "vehicle_no": "",
        "test_location": "st marks road", "testing_officer": "B 05",
        "testing_mode": "Passive", "test_result": "No Alcohol",
        "alcohol_bac": 0, "cannabis_ppb": 0,
        "alcohol_flag": "NO", "cannabis_flag": "NO", "cannabis_ratio": 0,
        "mobile_no": "", "address": "",
    }


def print_one_line_raw() -> tuple[bool, str]:
    """Rawest possible path - identical to `printf ... | tee /dev/ttyUSB0`."""
    payload = b"\x1b@" + b"BreathCheck printer test\n" + bytes([0x1B, 0x64, 5])
    print(f"TX ({len(payload)} bytes): {payload.hex(' ')}")
    try:
        with open(config.PRINTER_DEVICE, "wb", buffering=0) as port:
            port.write(payload)
            port.flush()
            os.fsync(port.fileno())
    except OSError as exc:
        return False, f"could not write to {config.PRINTER_DEVICE}: {exc}"
    return True, "sent (raw)"


def print_one_line() -> tuple[bool, str]:
    """Minimal path: open, INIT, one line, feed, flush, close."""
    try:
        from escpos.printer import Serial
    except ImportError:
        return False, "python-escpos not installed"
    try:
        device = Serial(
            devfile=config.PRINTER_DEVICE, baudrate=config.PRINTER_BAUD,
            bytesize=8, parity="N", stopbits=1, timeout=config.PRINTER_TIMEOUT,
        )
    except Exception as exc:
        return False, f"could not open {config.PRINTER_DEVICE}: {exc}"

    original_write = device.device.write

    def logged_write(data, _write=original_write):
        print(f"TX ({len(data)} bytes): {data.hex(' ')}")
        return _write(data)

    device.device.write = logged_write
    try:
        device.hw("INIT")
        device.text("BreathCheck printer test\n")
        device.device.write(bytes([0x1B, 0x64, 5]))
        device.device.flush()
        return True, "sent"
    except Exception as exc:
        return False, f"failed: {exc}"
    finally:
        try:
            device.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Printer test")
    parser.add_argument("--line", action="store_true",
                        help="send a single line instead of a full receipt")
    parser.add_argument("--raw", action="store_true",
                        help="write bytes straight to the device (like tee)")
    args = parser.parse_args()
    if args.raw:
        config.PRINTER_MODE = "raw"

    print(f"config : {ENV_FILE}")
    print(f"mode   : {config.PRINTER_MODE}")
    print(f"device : {config.PRINTER_DEVICE} @ {config.PRINTER_BAUD} baud, "
          f"timeout {config.PRINTER_TIMEOUT}s")
    if config.PRINTER_MODE not in ("serial", "raw"):
        print(f"\n!! HH_PRINTER_MODE is '{config.PRINTER_MODE}', so nothing reaches the printer.")
        print(f"   Set HH_PRINTER_MODE=raw in {ENV_FILE}, or run with:")
        print("     sudo HH_PRINTER_MODE=raw .venv/bin/python radxa/print-test.py")

    config.PRINTER_DEBUG = True   # always show the bytes for a test print
    print()
    if args.line:
        ok, message = print_one_line_raw() if config.PRINTER_MODE == "raw" \
            else print_one_line()
    else:
        ok, message = printer.print_record(sample_record(), {})
    print(f"\nresult: {message}")
    if ok:
        print("If no paper came out, the bytes above left the Pi but the printer")
        print("ignored them: check power, paper, and the baud rate.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
