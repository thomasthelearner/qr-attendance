"""
database.py
-----------
Handles all SQLite setup and connection management.

The database is a single local file (attendance.db) that lives next to
this script. No external database server is required — this satisfies
the "local-first" requirement of the system.

SESSIONS
--------
Attendance is organized into "sessions" (e.g. one per class, one per
event). Only one session is "active" at a time. QR codes and check-ins
are always tied to whichever session is active — starting a new session
automatically retires the old one, and everyone (including devices that
already checked in) becomes eligible again under the new session. Past
sessions' data is kept, just no longer live.
"""

import sqlite3
import secrets
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime

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


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    """
    Creates the sessions + attendance tables if they don't exist yet, and
    migrates a pre-sessions database (from earlier versions of this app)
    into the new schema without losing any previously collected data.
    """
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                is_active   INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()

        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='attendance'"
        ).fetchone()

        if not table_exists:
            # Fresh install — create the current schema directly.
            # NOTE: id_number is unique per SESSION (enforced in app.py),
            # not globally, since the same person checks in across many
            # sessions over time.
            conn.execute(
                """
                CREATE TABLE attendance (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  INTEGER NOT NULL,
                    full_name   TEXT NOT NULL,
                    id_number   TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'Absent'
                                CHECK (status IN ('Absent', 'Present')),
                    timestamp   TEXT,
                    method      TEXT DEFAULT 'Manual',
                    source_ip   TEXT,
                    device_id   TEXT
                )
                """
            )
            conn.commit()
            return

        existing_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(attendance)")
        }
        if "session_id" in existing_columns:
            return  # already on the current schema, nothing to migrate

        # --- One-time migration: pre-sessions database found ---
        # Move all existing rows into a single "Legacy Data" session so
        # nothing collected before this update is lost, then rebuild the
        # table under the new (per-session) schema.
        legacy_cursor = conn.execute(
            "INSERT INTO sessions (name, created_at, is_active) VALUES (?, ?, 0)",
            ("Legacy Data (before sessions existed)", _now()),
        )
        legacy_session_id = legacy_cursor.lastrowid

        conn.execute("ALTER TABLE attendance RENAME TO attendance_old")
        conn.execute(
            """
            CREATE TABLE attendance (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  INTEGER NOT NULL,
                full_name   TEXT NOT NULL,
                id_number   TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'Absent'
                            CHECK (status IN ('Absent', 'Present')),
                timestamp   TEXT,
                method      TEXT DEFAULT 'Manual',
                source_ip   TEXT,
                device_id   TEXT
            )
            """
        )
        old_rows = conn.execute("SELECT * FROM attendance_old").fetchall()
        old_columns = old_rows[0].keys() if old_rows else []
        for r in old_rows:
            conn.execute(
                """INSERT INTO attendance
                   (session_id, full_name, id_number, status, timestamp, method, source_ip, device_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    legacy_session_id,
                    r["full_name"],
                    r["id_number"],
                    r["status"],
                    r["timestamp"],
                    r["method"],
                    r["source_ip"] if "source_ip" in old_columns else None,
                    r["device_id"] if "device_id" in old_columns else None,
                ),
            )
        conn.execute("DROP TABLE attendance_old")
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


# ---------------------------------------------------------------------------
# SESSIONS
# ---------------------------------------------------------------------------
def get_active_session() -> dict | None:
    """Returns the currently active session, or None if none is running."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def create_session(name: str) -> dict:
    """
    Starts a new session and automatically deactivates any other active
    session. This is what makes every device "eligible again" — the
    device/IP duplicate check is always scoped to the CURRENT session.
    """
    with get_connection() as conn:
        conn.execute("UPDATE sessions SET is_active = 0")
        cursor = conn.execute(
            "INSERT INTO sessions (name, created_at, is_active) VALUES (?, ?, 1)",
            (name, _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)


def end_active_session() -> None:
    """Stops accepting check-ins without starting a replacement session."""
    with get_connection() as conn:
        conn.execute("UPDATE sessions SET is_active = 0 WHERE is_active = 1")
        conn.commit()


def list_sessions() -> list[dict]:
    """All sessions, newest first, each with a count of present check-ins."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM attendance a
                     WHERE a.session_id = s.id AND a.status = 'Present') AS present_count
            FROM sessions s
            ORDER BY s.id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def delete_session(session_id: int) -> bool:
    """
    Permanently deletes a session and every attendance record that
    belongs to it. Returns False if the session didn't exist.
    """
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not exists:
            return False
        conn.execute("DELETE FROM attendance WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return True


# ---------------------------------------------------------------------------
# ATTENDANCE RECORDS (scoped to a single session)
# ---------------------------------------------------------------------------
def fetch_all_records(session_id: int | None) -> list[dict]:
    """
    Returns every attendance record for ONE session, most recently updated
    first, with an added "flagged" field: True if this record's device/IP
    was also used to check in a DIFFERENT person within the SAME session.
    """
    if session_id is None:
        return []

    with get_connection() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM attendance WHERE session_id = ? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        ]

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
