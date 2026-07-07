"""
database.py
-----------
Handles all SQLite setup and connection management.

The database is a single local file (attendance.db) that lives next to
this script. No external database server is required — this satisfies
the "local-first" requirement of the system.
"""

import sqlite3
import secrets
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "attendance.db"
SECRET_FILE = BASE_DIR / ".secret_key"  # persisted HMAC secret (gitignore this!)


@contextmanager
def get_connection():
    """Context-managed SQLite connection with dict-like row access."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Creates the attendance table if it doesn't already exist, and
    migrates older databases to add the anti-duplicate-device columns."""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name   TEXT NOT NULL,
                id_number   TEXT NOT NULL UNIQUE,
                status      TEXT NOT NULL DEFAULT 'Absent'
                            CHECK (status IN ('Absent', 'Present')),
                timestamp   TEXT,               -- check-in time, NULL if Absent
                method      TEXT DEFAULT 'Manual',  -- 'QR Scan' or 'Manual'
                source_ip   TEXT,               -- LAN IP that submitted the check-in
                device_id   TEXT                -- random per-browser ID (localStorage)
            )
            """
        )
        conn.commit()

        # --- Lightweight migration for databases created before this update ---
        existing_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(attendance)")
        }
        if "source_ip" not in existing_columns:
            conn.execute("ALTER TABLE attendance ADD COLUMN source_ip TEXT")
        if "device_id" not in existing_columns:
            conn.execute("ALTER TABLE attendance ADD COLUMN device_id TEXT")
        conn.commit()


def get_or_create_secret_key() -> bytes:
    """
    Generates a random 32-byte secret on first run and persists it to disk
    so the token sequence stays stable across server restarts. If this
    file is deleted, all previously issued QR codes become invalid
    immediately (which is fine — a new one is generated right away).
    """
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()

    new_key = secrets.token_bytes(32)
    SECRET_FILE.write_bytes(new_key)
    return new_key


def fetch_all_records() -> list[dict]:
    """
    Returns every attendance record, most recently updated first, with an
    added "flagged" field: True if this record's check-in device/IP was
    also used to check in a DIFFERENT person. This doesn't block anything
    by itself (blocking happens in app.py at check-in time) — it just
    gives the admin a visual heads-up on the ledger for anything that
    slipped through before this feature existed, or for manual review.
    """
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM attendance ORDER BY id DESC"
        ).fetchall()]

    # Count how many DISTINCT id_numbers share each non-null device_id / source_ip
    device_to_ids = {}
    ip_to_ids = {}
    for r in rows:
        if r["status"] != "Present":
            continue
        if r["device_id"]:
            device_to_ids.setdefault(r["device_id"], set()).add(r["id_number"])
        if r["source_ip"]:
            ip_to_ids.setdefault(r["source_ip"], set()).add(r["id_number"])

    for r in rows:
        flagged_by_device = r["device_id"] and len(device_to_ids.get(r["device_id"], set())) > 1
        flagged_by_ip = r["source_ip"] and len(ip_to_ids.get(r["source_ip"], set())) > 1
        r["flagged"] = bool(flagged_by_device or flagged_by_ip)

    return rows
