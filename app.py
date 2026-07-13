"""
app.py
------
Main FastAPI application for the local-first QR Attendance system.

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8000
(or, if you've set up local HTTPS with mkcert, use ./run_https.sh instead)

Then on the HOST machine, open:
    http://localhost:8000/admin

On CLIENT devices connected to the SAME Wi-Fi/LAN, open:
    http://<host-machine-LAN-IP>:8000/scan

SESSIONS
--------
Check-ins only happen inside a "session" that the admin explicitly starts
(e.g. "Monday Lecture"). Each QR code has the active session's ID baked
directly into it alongside the usual 10-second rotating token, so a code
from an ended session is rejected even if someone still has it on screen.
Starting a NEW session automatically makes every device eligible to check
in again — the one-device-one-checkin rule is scoped per session, not
global or permanent.
"""

import asyncio
import base64
import csv
import io
from datetime import datetime
from pathlib import Path

import qrcode
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import database
import security

BASE_DIR = Path(__file__).parent

app = FastAPI(title="QR Attendance System")

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---------------------------------------------------------------------------
# WEBSOCKET CONNECTION MANAGER
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast_json(self, payload: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(payload)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# QR IMAGE GENERATION
# ---------------------------------------------------------------------------
def build_qr_payload() -> dict:
    """
    Generates the QR code that should be displayed RIGHT NOW, if any.
    If no session is active, returns a payload with qr_image=None so the
    admin page can show a "start a session" placeholder instead.
    """
    session = database.get_active_session()

    if not session:
        return {
            "type": "qr",
            "active_session": None,
            "qr_image": None,
            "raw_token": None,
            "expires_in": None,
            "step_seconds": security.TIME_STEP,
        }

    token_info = security.generate_current_token()
    # The QR payload combines the session ID with the rotating time-hashed
    # token: "<session_id>:<token>". This means a code from a session that
    # has since ended is rejected outright at check-in time, independent
    # of whether the 10-second token itself would still "look" valid.
    combined_token = f"{session['id']}:{token_info['token']}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(combined_token)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")

    return {
        "type": "qr",
        "active_session": {"id": session["id"], "name": session["name"]},
        "qr_image": f"data:image/png;base64,{encoded}",
        "raw_token": combined_token,  # shown small on-screen for manual-entry testing
        "expires_in": token_info["expires_in"],
        "step_seconds": token_info["step_seconds"],
    }


def validate_scanned_token(raw_token: str, active_session_id: int) -> bool:
    """
    Validates a token submitted by a scanning client: it must be in the
    "<session_id>:<time_token>" format, the session_id must match the
    CURRENTLY active session, and the time_token itself must still be
    within its rotation window.
    """
    if not raw_token or ":" not in raw_token:
        return False
    session_part, time_part = raw_token.split(":", 1)
    try:
        token_session_id = int(session_part)
    except ValueError:
        return False
    if token_session_id != active_session_id:
        return False
    return security.validate_token(time_part)


# ---------------------------------------------------------------------------
# BACKGROUND TASK: rotate + broadcast the QR code
# ---------------------------------------------------------------------------
async def qr_rotation_loop():
    while True:
        payload = build_qr_payload()
        await manager.broadcast_json(payload)
        if payload["expires_in"] is None:
            # No active session — just check back periodically instead of
            # spinning as fast as possible.
            await asyncio.sleep(2)
        else:
            await asyncio.sleep(payload["expires_in"])


@app.on_event("startup")
async def on_startup():
    database.init_db()
    security.set_secret_key(database.get_or_create_secret_key())
    app.state.qr_task = asyncio.create_task(qr_rotation_loop())


async def broadcast_attendance_update():
    """Pushes the CURRENT session's attendance list to all connected admins."""
    session = database.get_active_session()
    records = database.fetch_all_records(session["id"] if session else None)
    await manager.broadcast_json(
        {"type": "attendance", "records": records, "session_id": session["id"] if session else None}
    )


# ---------------------------------------------------------------------------
# PAGE ROUTES
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return HTMLResponse(
        """
        <html><body style="font-family: sans-serif; padding: 2rem;">
        <h2>QR Attendance System</h2>
        <p><a href="/admin">Go to Admin Panel</a></p>
        <p><a href="/scan">Go to Client Check-in Page</a></p>
        </body></html>
        """
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})


@app.get("/scan", response_class=HTMLResponse)
async def scan_page(request: Request):
    return templates.TemplateResponse("scan.html", {"request": request})


# ---------------------------------------------------------------------------
# WEBSOCKET ENDPOINT (used by /admin only)
# ---------------------------------------------------------------------------
@app.websocket("/ws/admin")
async def admin_websocket(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_json(build_qr_payload())
        session = database.get_active_session()
        await websocket.send_json(
            {
                "type": "attendance",
                "records": database.fetch_all_records(session["id"] if session else None),
                "session_id": session["id"] if session else None,
            }
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# REQUEST / RESPONSE MODELS
# ---------------------------------------------------------------------------
class CheckinRequest(BaseModel):
    full_name: str
    id_number: str
    token: str
    device_id: str = ""


class ManualAddRequest(BaseModel):
    full_name: str
    id_number: str
    status: str = "Absent"


class StatusUpdateRequest(BaseModel):
    status: str


class SessionCreateRequest(BaseModel):
    name: str = ""


# ---------------------------------------------------------------------------
# API: SESSION MANAGEMENT
# ---------------------------------------------------------------------------
@app.post("/api/sessions/start")
async def start_session(payload: SessionCreateRequest):
    name = payload.name.strip() or f"Session — {datetime.now().strftime('%b %d, %I:%M %p')}"
    session = database.create_session(name)
    # Broadcast immediately rather than waiting for the loop's next tick,
    # so the admin screen updates the instant "Start" is clicked.
    await manager.broadcast_json(build_qr_payload())
    await broadcast_attendance_update()
    return {"success": True, "session": session}


@app.post("/api/sessions/end")
async def end_session():
    database.end_active_session()
    await manager.broadcast_json(build_qr_payload())
    await broadcast_attendance_update()
    return {"success": True}


@app.get("/api/sessions")
async def get_sessions():
    return {
        "sessions": database.list_sessions(),
        "active": database.get_active_session(),
    }


@app.get("/api/records")
async def get_records(session_id: int):
    """Used by the admin 'Past Sessions' viewer to load an archived session
    into the ledger, read-only, without disturbing the live session view."""
    return {"records": database.fetch_all_records(session_id)}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: int):
    """Deletes a session and all of its attendance records permanently.
    Refuses to delete whatever session is currently active — end it first."""
    active = database.get_active_session()
    if active and active["id"] == session_id:
        raise HTTPException(
            status_code=400,
            detail="Can't delete the active session — end it first, then delete it.",
        )
    deleted = database.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"success": True}


# ---------------------------------------------------------------------------
# API: CLIENT CHECK-IN (token-validated, session-scoped)
# ---------------------------------------------------------------------------
@app.post("/api/checkin")
async def checkin(payload: CheckinRequest, request: Request):
    full_name = payload.full_name.strip()
    id_number = payload.id_number.strip()
    device_id = payload.device_id.strip()
    source_ip = request.client.host if request.client else ""

    if not full_name or not id_number:
        raise HTTPException(status_code=400, detail="Name and ID are required.")

    session = database.get_active_session()
    if not session:
        return {
            "success": False,
            "message": "No check-in session is currently active. Please wait for a staff member to start one.",
        }
    session_id = session["id"]

    # --- ANTI-CHEAT CHECK #1: token belongs to THIS session and is time-valid ---
    if not validate_scanned_token(payload.token, session_id):
        return {
            "success": False,
            "message": "QR code expired, invalid, or from a different session. Please scan the current code on screen and try again.",
        }

    # --- ANTI-CHEAT CHECK #2: has this device already checked someone else in, THIS SESSION? ---
    # Scoped to session_id, so a new session automatically clears this for
    # every device — no manual "reset" is ever needed or offered.
    with database.get_connection() as conn:
        conflict = conn.execute(
            """SELECT id_number FROM attendance
               WHERE session_id = ? AND status = 'Present' AND id_number != ?
                 AND ((device_id != '' AND device_id = ?) OR (source_ip != '' AND source_ip = ?))
               LIMIT 1""",
            (session_id, id_number, device_id, source_ip),
        ).fetchone()

    if conflict:
        return {
            "success": False,
            "message": "This device has already checked someone else in for this session. Each device can only check in one person per session.",
        }

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with database.get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM attendance WHERE session_id = ? AND id_number = ?",
            (session_id, id_number),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE attendance
                   SET full_name = ?, status = 'Present', timestamp = ?, method = 'QR Scan',
                       source_ip = ?, device_id = ?
                   WHERE session_id = ? AND id_number = ?""",
                (full_name, now, source_ip, device_id, session_id, id_number),
            )
        else:
            conn.execute(
                """INSERT INTO attendance
                   (session_id, full_name, id_number, status, timestamp, method, source_ip, device_id)
                   VALUES (?, ?, ?, 'Present', ?, 'QR Scan', ?, ?)""",
                (session_id, full_name, id_number, now, source_ip, device_id),
            )
        conn.commit()

    await broadcast_attendance_update()

    return {
        "success": True,
        "message": f"Welcome, {full_name}! You're checked in for \"{session['name']}\" at {now}.",
    }


# ---------------------------------------------------------------------------
# API: ADMIN CRUD OPERATIONS (scoped to the active session)
# ---------------------------------------------------------------------------
@app.post("/api/manual_add")
async def manual_add(payload: ManualAddRequest):
    session = database.get_active_session()
    if not session:
        raise HTTPException(status_code=400, detail="Start a session before adding records.")
    session_id = session["id"]

    full_name = payload.full_name.strip()
    id_number = payload.id_number.strip()
    status = payload.status if payload.status in ("Absent", "Present") else "Absent"

    if not full_name or not id_number:
        raise HTTPException(status_code=400, detail="Name and ID are required.")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status == "Present" else None

    with database.get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM attendance WHERE session_id = ? AND id_number = ?",
            (session_id, id_number),
        ).fetchone()
        if existing:
            raise HTTPException(
                status_code=409, detail="A record with this ID number already exists in this session."
            )
        conn.execute(
            """INSERT INTO attendance (session_id, full_name, id_number, status, timestamp, method)
               VALUES (?, ?, ?, ?, ?, 'Manual')""",
            (session_id, full_name, id_number, status, timestamp),
        )
        conn.commit()

    await broadcast_attendance_update()
    return {"success": True}


@app.put("/api/update_status/{record_id}")
async def update_status(record_id: int, payload: StatusUpdateRequest):
    if payload.status not in ("Absent", "Present"):
        raise HTTPException(status_code=400, detail="Invalid status value.")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if payload.status == "Present" else None

    with database.get_connection() as conn:
        result = conn.execute(
            "UPDATE attendance SET status = ?, timestamp = ?, method = 'Manual' WHERE id = ?",
            (payload.status, timestamp, record_id),
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Record not found.")

    await broadcast_attendance_update()
    return {"success": True}


@app.delete("/api/delete/{record_id}")
async def delete_record(record_id: int):
    with database.get_connection() as conn:
        result = conn.execute("DELETE FROM attendance WHERE id = ?", (record_id,))
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Record not found.")

    await broadcast_attendance_update()
    return {"success": True}


# ---------------------------------------------------------------------------
# API: CSV EXPORT (defaults to the active session; pass ?session_id=N for a past one)
# ---------------------------------------------------------------------------
@app.get("/api/export_csv")
async def export_csv(session_id: int | None = None):
    if session_id is None:
        active = database.get_active_session()
        if not active:
            raise HTTPException(status_code=400, detail="No active session — specify ?session_id=N instead.")
        session_id = active["id"]

    records = database.fetch_all_records(session_id)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["ID", "Full Name", "ID Number", "Status", "Timestamp", "Method", "Source IP", "Device ID", "Flagged"]
    )
    for r in records:
        writer.writerow(
            [
                r["id"], r["full_name"], r["id_number"], r["status"], r["timestamp"],
                r["method"], r.get("source_ip", ""), r.get("device_id", ""), r.get("flagged", False),
            ]
        )
    buffer.seek(0)

    filename = f"attendance_export_session{session_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
