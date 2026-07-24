"""BreathCheck terminal client — drive the analyzer over SSH, no browser.

Runs the same measurement cycle and stores to the same SQLite database as the
web app. Started with `python app.py term`. Menu-driven: scan, database, GPS,
settings.

Only one process may hold the GPIO/SPI at a time, so stop the background
service first (radxa/term.sh does this for you): sudo systemctl stop breathcheck
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime

from src import analyzer as analyzer_module
from src import config, db, scan
from src.gps import GpsProvider

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def bold(t: str) -> str: return _c("1", t)
def dim(t: str) -> str: return _c("2", t)
def green(t: str) -> str: return _c("32;1", t)
def red(t: str) -> str: return _c("31;1", t)
def cyan(t: str) -> str: return _c("36;1", t)
def amber(t: str) -> str: return _c("33;1", t)


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except EOFError:
        return default
    return value or default


def _flag(text: str, failed: bool) -> str:
    return red(f"{text} FAIL") if failed else green(f"{text} PASS")


class Terminal:
    def __init__(self) -> None:
        db.init_db()
        self.analyzer = analyzer_module.resolve_analyzer()
        self.gps = GpsProvider()
        if self.analyzer.name == "spi":
            # Prime the alcohol cell in the background, like the web server.
            self.analyzer.state = "stabilizing"
            self.analyzer.stabilize_started_at = time.time()
            threading.Thread(target=self.analyzer.stabilize, daemon=True).start()

    # ---- menu ------------------------------------------------------------

    def run(self) -> None:
        for warning in self.analyzer.startup_warnings:
            print(amber(f"! {warning}"))
        while True:
            self._header()
            choice = _prompt("Choose [S]can [D]atabase [G]ps [C]settings [Q]uit").lower()
            if choice in ("s", "scan"):
                self.scan()
            elif choice in ("d", "database", "db"):
                self.database()
            elif choice in ("g", "gps"):
                self.gps_screen()
            elif choice in ("c", "settings", "set"):
                self.settings_screen()
            elif choice in ("q", "quit", "exit"):
                print("Bye.")
                return
            else:
                print(dim("  (unknown option)"))

    def _header(self) -> None:
        settings = db.get_settings()
        sensor = self.analyzer.name.upper()
        if self.analyzer.state == "stabilizing":
            sensor += " (WARMING UP)"
        print()
        print(bold("=" * 52))
        print(bold(f" {config.APP_NAME} {config.APP_VERSION}") +
              dim(f"   SET {settings.get('set_no', '--')}  |  "
                  f"{db.count_records()} records  |  SENSOR {sensor}"))
        print(bold("=" * 52))

    # ---- scan ------------------------------------------------------------

    def scan(self) -> None:
        if self.analyzer.state == "stabilizing":
            elapsed = time.time() - getattr(self.analyzer, "stabilize_started_at", time.time())
            print(amber(f"  Sensor still warming up ({elapsed:.0f}s). Try again shortly."))
            return
        if self.analyzer.state == "measuring":
            print(amber("  A test is already running."))
            return

        settings = db.get_settings()
        measure_seconds = max(3, int(float(settings.get("scan_seconds", "10"))))
        counter = db.next_counter()
        now = config.now_local()
        receipt_id = scan.new_receipt(counter, now)
        fix = self.gps.read()

        print()
        print(cyan(f"  Receipt {receipt_id}   cycle: purge -> baseline -> BLOW"))
        state = {"phase": None}

        def progress(phase: str, elapsed: float, total: float) -> None:
            if phase != state["phase"]:
                state["phase"] = phase
                sys.stdout.write("\n")
                if phase == "measure":
                    sys.stdout.write("  " + green(">>> BLOW NOW <<<") + "\n")
            width = 24
            filled = int(width * min(1.0, elapsed / total)) if total else width
            bar = "#" * filled + "-" * (width - filled)
            label = {"purge": "PURGE", "baseline": "BASELINE", "measure": "BLOW"}.get(phase, phase.upper())
            sys.stdout.write(f"\r  {label:9} [{bar}] {elapsed:4.1f}/{total:4.1f}s ")
            sys.stdout.flush()

        try:
            cycle = self.analyzer.run_cycle(measure_seconds, progress)
        except Exception as exc:
            print("\n" + red(f"  Sensor error: {exc}"))
            return
        print()

        result = scan.build_result(cycle, settings, now)
        result["curve_file"] = scan.save_curve(receipt_id, cycle)
        self._print_result(result)

        if result.get("baseline_stable") is False:
            print(amber("  ! baseline unstable — result suspect"))

        session = {
            "receipt_id": receipt_id,
            "counter": counter,
            "area": settings.get("area", ""),
            "version": settings.get("version", config.APP_VERSION),
            "set_no": settings.get("set_no", ""),
            "calibr_date": settings.get("calibr_date", ""),
            "testing_mode": settings.get("testing_mode", "ACTIVE"),
            "gps1": str(fix["lat"]) if fix.get("fix") else "",
            "gps2": str(fix["lon"]) if fix.get("fix") else "",
        }
        self._save_prompt(result, session, settings)

    def _print_result(self, result: dict) -> None:
        print(bold("  " + "-" * 40))
        alcohol_disp = f"{result['alcohol_bac']:.2f} mV.s"
        cannabis_disp = f"{result['cannabis_ppb']:.2f} mV.s"
        print("  ALCOHOL   " + f"{alcohol_disp:>14}   " +
              _flag("", result["alcohol_flag"] == "YES"))
        print("  CANNABIS  " + f"{cannabis_disp:>14}   " +
              _flag("", result["cannabis_flag"] == "YES"))
        ratio_disp = f"{result.get('cannabis_ratio', 0.0):.3f}"
        print("  U/L RATIO " + f"{ratio_disp:>14}   " +
              f"(thr {result.get('cannabis_threshold', 0)} mV, "
              f"{result.get('cannabis_points', 0)} pts)")
        verdict = result["test_result"]
        print("  VERDICT   " +
              (green(verdict) if verdict == "PASS" else red(verdict)))
        print(bold("  " + "-" * 40))

    def _save_prompt(self, result: dict, session: dict, settings: dict) -> None:
        name = _prompt("  Subject name (blank to discard)")
        if not name:
            print(dim("  discarded — not saved"))
            return
        fields = {
            "name": name,
            "dl_number": _prompt("  DL no"),
            "vehicle_no": _prompt("  Vehicle no"),
            "mobile_no": _prompt("  Mobile no"),
            "test_location": _prompt("  Test location"),
            "testing_officer": _prompt("  Officer", settings.get("officer", "")),
            "address": _prompt("  Address"),
        }
        record = scan.record_from_result(result, session, fields)
        try:
            db.insert_record(record)
        except Exception as exc:
            print(red(f"  Could not save: {exc}"))
            return
        print(green(f"  Saved {session['receipt_id']}"))

    # ---- database --------------------------------------------------------

    def database(self) -> None:
        query = _prompt("  Search (blank = latest)")
        rows = db.list_records(query.strip())
        if not rows:
            print(dim("  no records"))
            return
        print()
        print(bold(f"  {'NAME':<20}{'DL NO':<18}{'ALC':<6}{'CAN':<6}{'DATE':<12}"))
        print(dim("  " + "-" * 60))
        for row in rows[:50]:
            alc = red("YES") if row["alcohol_flag"] == "YES" else green("NO ")
            can = red("YES") if row["cannabis_flag"] == "YES" else green("NO ")
            name = (row["name"] or "--")[:19]
            dl = (row["dl_number"] or "--")[:17]
            print(f"  {name:<20}{dl:<18}{alc:<6}{can:<6}{row['test_date']:<12}")
        print(dim(f"  ({len(rows)} shown)"))

    # ---- gps -------------------------------------------------------------

    def gps_screen(self) -> None:
        fix = self.gps.read()
        print()
        if fix.get("fix"):
            print("  " + green("FIX OK") + f"   {fix['sats']} sat   {fix.get('updated_at', '--')}")
            print(f"  LAT {fix['lat']:.6f}")
            print(f"  LON {fix['lon']:.6f}")
        else:
            print("  " + red("NO FIX") + dim(f"   (mode: {fix.get('mode', config.GPS_MODE)})"))

    # ---- settings --------------------------------------------------------

    def settings_screen(self) -> None:
        settings = db.get_settings()
        print()
        print(bold("  DEVICE / LIMITS"))
        for key in ("set_no", "area", "calibr_date", "testing_mode", "officer",
                    "alcohol_limit", "cannabis_limit", "scan_seconds"):
            print(f"    {key:<16} {settings.get(key, '')}")
        print(dim("  Edit a value: type key=value (e.g. alcohol_limit=12). Enter to go back."))
        editable = {"set_no", "area", "calibr_date", "testing_mode", "officer",
                    "alcohol_limit", "cannabis_limit", "scan_seconds"}
        while True:
            line = _prompt("  edit")
            if not line:
                return
            if "=" not in line:
                print(dim("  format: key=value"))
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key not in editable:
                print(dim(f"  not editable: {key}"))
                continue
            db.set_settings({key: value})
            print(green(f"  {key} = {value}"))


def run() -> None:
    try:
        Terminal().run()
    except KeyboardInterrupt:
        print("\nBye.")
