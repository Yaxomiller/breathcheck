"""Thermal receipt printing over an ESC/POS serial printer.

After a test is saved, the officer can print a receipt. The printer is a
serial ESC/POS device on the handheld (e.g. /dev/ttyUSB0), so this can only
run on the backend. Layout follows the field order used by the existing
A8080-style receipts, filled with the app's real record data.

Modes (HH_PRINTER_MODE): "serial" on the device, "mock" logs the receipt on a
dev machine, "off" disables it. Everything degrades gracefully — a missing
printer or python-escpos returns (False, reason) rather than raising.
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
    """The (label, value) lines for the receipt body, in print order."""
    serial_number = _v(config.PRINT_SERIAL_NUMBER) or _v(record.get("set_no")) \
        or _v(settings.get("set_no"))
    alcohol_state = "FAIL" if record.get("alcohol_flag") == "YES" else "PASS"
    cannabis_state = "FAIL" if record.get("cannabis_flag") == "YES" else "PASS"
    try:
        ratio = f"{float(record.get('cannabis_ratio') or 0):.3f}"
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
        ("Name: ", _v(record.get("name"))),
        ("DL Number: ", _v(record.get("dl_number"))),
        ("Vehicle Number: ", _v(record.get("vehicle_no"))),
        ("Test Location: ", _v(record.get("test_location"))),
        ("Testing Officer: ", _v(record.get("testing_officer"))),
        ("Test mode: ", _v(record.get("testing_mode"), "ACTIVE")),
        ("Test result: ", _v(record.get("test_result"), "NA")),
        ("Alcohol: ", f"{_v(record.get('alcohol_bac'), '0')} mV.s ({alcohol_state})"),
        ("Cannabis: ", f"{_v(record.get('cannabis_ppb'), '0')} mV.s ({cannabis_state})"),
        ("U/L Ratio: ", ratio),
        ("Mobile Number: ", _v(record.get("mobile_no"))),
        ("Address: ", _v(record.get("address"))),
    ]


def build_receipt_text(record: dict, settings: dict) -> str:
    """Plain-text rendering — used for mock mode and logging."""
    lines = [
        config.PRINT_DEVICE_NAME,
        *( [config.PRINT_STATION_NAME] if config.PRINT_STATION_NAME else [] ),
        *( [config.PRINT_STATION_ADDRESS] if config.PRINT_STATION_ADDRESS else [] ),
        "",
    ]
    lines += [f"{label}{value}" for label, value in receipt_fields(record, settings)]
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
        return False, f"Printer not found on {config.PRINTER_DEVICE}"

    try:
        printer.hw("INIT")

        printer.set(align="center", bold=True, width=2, height=2)
        printer.text(config.PRINT_DEVICE_NAME + "\n")

        printer.set(align="center", bold=False, width=1, height=1)
        if config.PRINT_STATION_NAME:
            printer.text(config.PRINT_STATION_NAME + "\n")
        if config.PRINT_STATION_ADDRESS:
            printer.text(config.PRINT_STATION_ADDRESS + "\n")
        printer.text("\n")

        printer.set(align="left")
        for label, value in receipt_fields(record, settings):
            printer.text(f"{label}{value}\n")

        printer.device.write(bytes([0x1B, 0x64, max(0, config.PRINTER_FEED_LINES)]))
        printer.device.flush()
        return True, "Printed"
    except Exception as exc:
        logger.warning("print failed: %s", exc)
        return False, f"Print failed: {exc}"
    finally:
        try:
            printer.close()
        except Exception:
            pass
