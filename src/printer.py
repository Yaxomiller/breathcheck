"""Thermal receipt printing over an ESC/POS serial printer.

After a test is saved, the officer can print a receipt. The printer is a
serial ESC/POS device on the handheld (e.g. /dev/ttyUSB0), so this can only
run on the backend. Layout follows the A8080-style receipt: a large centred
device name, the station lines, then bold labels with plain values.

Modes (HH_PRINTER_MODE): "serial" on the device, "mock" logs the receipt on a
dev machine, "off" disables it. Everything degrades gracefully -- a missing
printer or python-escpos returns (False, reason) rather than raising.

Sample print (verify the printer without running a breath test):

    python app.py testprint
"""
from __future__ import annotations

import logging
from typing import Any

from src import config

logger = logging.getLogger("breathcheck.printer")


def _v(value: Any, blank: str = "") -> str:
    text = "" if value is None else str(value).strip()
    return text if text else blank


def receipt_fields(record: dict, settings: dict) -> list[tuple[str, str]]:
    """The (label, value) lines for the receipt body, in print order.
    A ("", "") entry is a blank separator line."""
    serial_number = _v(config.PRINT_SERIAL_NUMBER) or _v(record.get("set_no")) \
        or _v(settings.get("set_no"))
    alcohol_state = "FAIL" if record.get("alcohol_flag") == "YES" else "PASS"
    cannabis_state = "FAIL" if record.get("cannabis_flag") == "YES" else "PASS"
    try:
        ratio = "%.3f" % float(record.get("cannabis_ratio") or 0)
    except (TypeError, ValueError):
        ratio = "0.000"

    return [
        ("Receipt: ", _v(record.get("receipt_id"), "NA")),
        ("Version: ", _v(record.get("version"), config.APP_VERSION)),
        ("Ser.-No: ", _v(serial_number, "NA")),
        ("", ""),
        ("Counter: ", _v(record.get("counter"), "0")),
        ("Date: ", _v(record.get("test_date"), "NA")),
        ("Time: ", _v(record.get("test_time"), "NA")),
        ("Calibr. Date: ", _v(record.get("calibr_date"), "NA")),
        ("", ""),
        ("GPS1: ", _v(record.get("gps1"), "NA")),
        ("GPS2: ", _v(record.get("gps2"), "NA")),
        ("Air Blank Test: ", _v(record.get("air_blank_test"), "0")),
        ("Name: ", _v(record.get("name"))),
        ("DL Number: ", _v(record.get("dl_number"))),
        ("Vehicle Number: ", _v(record.get("vehicle_no"))),
        ("Test Location: ", _v(record.get("test_location"))),
        ("Testing Officer: ", _v(record.get("testing_officer"))),
        ("Test mode: ", _v(record.get("testing_mode"), "ACTIVE")),
        ("Test result: ", _v(record.get("test_result"), "NA")),
        ("Alcohol: ", "%s mV.s (%s)" % (_v(record.get("alcohol_bac"), "0"), alcohol_state)),
        ("Cannabis: ", "%s mV.s (%s)" % (_v(record.get("cannabis_ppb"), "0"), cannabis_state)),
        ("U/L Ratio: ", ratio),
        ("Mobile Number: ", _v(record.get("mobile_no"))),
        ("Address: ", _v(record.get("address"))),
    ]


def sample_record() -> dict:
    """Reference data for a test print -- exercises the printer end to end
    without needing a real breath test."""
    now = config.now_local()
    return {
        "receipt_id": "SAMPLE-0001",
        "version": config.APP_VERSION,
        "set_no": "A8080T5130",
        "counter": 4965,
        "test_date": now.strftime("%Y/%m/%d"),
        "test_time": now.strftime("%H:%M:%S"),
        "calibr_date": "2026/05/16",
        "gps1": "NA",
        "gps2": "NA",
        "air_blank_test": 0,
        "name": "SAMPLE PRINT",
        "dl_number": "",
        "vehicle_no": "",
        "test_location": "st marks road",
        "testing_officer": "B 05",
        "testing_mode": "Passive",
        "test_result": "No Alcohol",
        "alcohol_bac": 0,
        "cannabis_ppb": 0,
        "alcohol_flag": "NO",
        "cannabis_flag": "NO",
        "cannabis_ratio": 0,
        "mobile_no": "",
        "address": "",
    }


def build_receipt_text(record: dict, settings: dict) -> str:
    """Plain-text rendering -- used for mock mode and logging."""
    lines = [config.PRINT_DEVICE_NAME]
    if config.PRINT_STATION_NAME:
        lines.append(config.PRINT_STATION_NAME)
    if config.PRINT_STATION_ADDRESS:
        lines.append(config.PRINT_STATION_ADDRESS)
    lines.append("")
    lines += ["%s%s" % (label, value) for label, value in receipt_fields(record, settings)]
    return "\n".join(lines)


def print_record(record: dict, settings: dict) -> tuple[bool, str]:
    """Print a saved record. Returns (ok, message)."""
    if config.PRINTER_MODE == "off":
        return False, "Printing is disabled"
    if config.PRINTER_MODE == "serial":
        return _print_serial(record, settings)
    logger.info("MOCK PRINT (HH_PRINTER_MODE=%s):\n%s",
                config.PRINTER_MODE, build_receipt_text(record, settings))
    return True, "Printed (mock)"


def print_sample() -> tuple[bool, str]:
    """Print the reference receipt. Used by `python app.py testprint`."""
    return print_record(sample_record(), {})


def _feed(printer: Any, lines: int) -> None:
    """Advance the paper. feed() exists in current python-escpos; older
    releases need the raw ESC d n command, which this board accepts."""
    if lines <= 0:
        return
    feed = getattr(printer, "feed", None)
    if callable(feed):
        try:
            feed(lines)
            return
        except Exception:
            pass   # fall through to the raw command
    printer.device.write(bytes([0x1B, 0x64, lines]))
    printer.device.flush()


def _print_serial(record: dict, settings: dict) -> tuple[bool, str]:
    try:
        from escpos.printer import Serial
    except ImportError:
        return False, "python-escpos not installed on this device"

    try:
        printer = Serial(
            devfile=config.PRINTER_DEVICE, baudrate=config.PRINTER_BAUD,
            bytesize=8, parity="N", stopbits=1, timeout=1,
        )
    except Exception as exc:
        logger.warning("printer open failed: %s", exc)
        return False, "Printer not found on %s" % config.PRINTER_DEVICE

    try:
        printer.hw("INIT")

        # Header: device name large, station lines beneath it.
        printer.set(align="center", bold=True, width=2, height=2)
        printer.text(config.PRINT_DEVICE_NAME + "\n")

        printer.set(align="center", bold=False, width=1, height=1)
        if config.PRINT_STATION_NAME:
            printer.text(config.PRINT_STATION_NAME + "\n")
        if config.PRINT_STATION_ADDRESS:
            printer.text(config.PRINT_STATION_ADDRESS + "\n")
        printer.text("\n")

        # Body: bold label, plain value.
        printer.set(align="left")
        for label, value in receipt_fields(record, settings):
            if not label and not value:
                printer.text("\n")
                continue
            printer.set(bold=True)
            printer.text(label)
            printer.set(bold=False)
            printer.text("%s\n" % value)

        _feed(printer, max(0, config.PRINTER_FEED_LINES))
        return True, "Printed"
    except Exception as exc:
        logger.warning("print failed: %s", exc)
        return False, "Print failed: %s" % exc
    finally:
        try:
            printer.close()
        except Exception:
            pass
