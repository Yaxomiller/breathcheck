"""SQLite storage for test records and persisted device settings."""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, Optional

from src import config

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT UNIQUE NOT NULL,
    area TEXT DEFAULT '',
    version TEXT DEFAULT '',
    set_no TEXT DEFAULT '',
    counter INTEGER DEFAULT 0,
    test_date TEXT DEFAULT '',
    test_time TEXT DEFAULT '',
    calibr_date TEXT DEFAULT '',
    gps1 TEXT DEFAULT '',
    gps2 TEXT DEFAULT '',
    name TEXT DEFAULT '',
    dl_number TEXT DEFAULT '',
    vehicle_no TEXT DEFAULT '',
    test_location TEXT DEFAULT '',
    testing_officer TEXT DEFAULT '',
    testing_mode TEXT DEFAULT '',
    test_result TEXT DEFAULT '',
    alcohol_bac REAL DEFAULT 0,
    cannabis_ppb REAL DEFAULT 0,
    alcohol_flag TEXT DEFAULT 'NO',
    cannabis_flag TEXT DEFAULT 'NO',
    mobile_no TEXT DEFAULT '',
    address TEXT DEFAULT '',
    photo_file TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_name ON records(name);
CREATE INDEX IF NOT EXISTS idx_records_dl ON records(dl_number);
"""

RECORD_FIELDS = (
    "receipt_id", "area", "version", "set_no", "counter",
    "test_date", "test_time", "calibr_date", "gps1", "gps2",
    "name", "dl_number", "vehicle_no", "test_location", "testing_officer",
    "testing_mode", "test_result", "alcohol_bac", "cannabis_ppb",
    "alcohol_baseline", "alcohol_peak", "cannabis_baseline", "cannabis_peak",
    "cannabis_ratio", "cannabis_upper", "cannabis_lower", "curve_file",
    "alcohol_flag", "cannabis_flag", "mobile_no", "address", "photo_file",
    "created_at",
)

# Columns added after the first release; applied with ALTER TABLE on upgrade.
# alcohol_baseline/peak are uA, cannabis_baseline/peak are mV.
_MIGRATION_COLUMNS = {
    "alcohol_baseline": "REAL DEFAULT 0",
    "alcohol_peak": "REAL DEFAULT 0",
    "cannabis_baseline": "REAL DEFAULT 0",
    "cannabis_peak": "REAL DEFAULT 0",
    # Upper/lower area split of the exhale curve (mV*s) and its trace file.
    "cannabis_ratio": "REAL DEFAULT 0",
    "cannabis_upper": "REAL DEFAULT 0",
    "cannabis_lower": "REAL DEFAULT 0",
    "curve_file": "TEXT DEFAULT ''",
}


def init_db() -> None:
    global _conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        for column, decl in _MIGRATION_COLUMNS.items():
            try:
                _conn.execute(f"ALTER TABLE records ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists
        # Limits from an older units scheme (raw ADC / mg) would silently
        # misjudge the new mV*s integrals — reset them once on upgrade.
        # Must run BEFORE the defaults are seeded, or the seeded
        # units_version would mask an upgraded database.
        row = _conn.execute("SELECT value FROM settings WHERE key = 'units_version'").fetchone()
        if row is None or row["value"] != config.UNITS_VERSION:
            _conn.execute("UPDATE settings SET value = ? WHERE key = 'alcohol_limit'",
                          (config.DEFAULT_ALCOHOL_LIMIT,))
            _conn.execute("UPDATE settings SET value = ? WHERE key = 'cannabis_limit'",
                          (config.DEFAULT_CANNABIS_LIMIT,))
            _conn.execute(
                "INSERT INTO settings(key, value) VALUES ('units_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (config.UNITS_VERSION,),
            )
        for key, value in config.DEFAULT_SETTINGS.items():
            _conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        _conn.commit()


def _db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("init_db() was not called")
    return _conn


# --- Settings ---------------------------------------------------------------

def get_settings() -> dict[str, str]:
    with _lock:
        rows = _db().execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def get_setting(key: str, default: str = "") -> str:
    with _lock:
        row = _db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_settings(values: dict[str, str]) -> None:
    with _lock:
        for key, value in values.items():
            _db().execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
        _db().commit()


def next_counter() -> int:
    """Atomically increment and return the device test counter."""
    with _lock:
        _db().execute(
            "UPDATE settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
            "WHERE key = 'counter'"
        )
        row = _db().execute("SELECT value FROM settings WHERE key = 'counter'").fetchone()
        _db().commit()
    return int(row["value"])


# --- Records ------------------------------------------------------------------

def insert_record(data: dict[str, Any]) -> int:
    columns = [field for field in RECORD_FIELDS if field in data]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO records ({', '.join(columns)}) VALUES ({placeholders})"
    with _lock:
        cursor = _db().execute(sql, [data[c] for c in columns])
        _db().commit()
    return int(cursor.lastrowid)


def list_records(query: str = "", limit: int = 500) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, receipt_id, name, dl_number, alcohol_flag, cannabis_flag, "
        "test_date, test_time FROM records"
    )
    params: list[Any] = []
    if query:
        sql += " WHERE name LIKE ? OR dl_number LIKE ? OR receipt_id LIKE ? OR vehicle_no LIKE ?"
        like = f"%{query}%"
        params = [like, like, like, like]
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _lock:
        rows = _db().execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_record(record_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = _db().execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    return dict(row) if row else None


def get_record_by_receipt(receipt_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        row = _db().execute(
            "SELECT * FROM records WHERE receipt_id = ? ORDER BY id DESC LIMIT 1",
            (receipt_id,),
        ).fetchone()
    return dict(row) if row else None


def all_records() -> list[dict[str, Any]]:
    with _lock:
        rows = _db().execute("SELECT * FROM records ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def count_records() -> int:
    with _lock:
        row = _db().execute("SELECT COUNT(*) AS n FROM records").fetchone()
    return int(row["n"])


def clear_records() -> int:
    with _lock:
        cursor = _db().execute("DELETE FROM records")
        _db().commit()
    return int(cursor.rowcount)
