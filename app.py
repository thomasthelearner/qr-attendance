"""
app.py
------
Main FastAPI application for the local-first QR Attendance system.

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8000

Then on the HOST machine, open:
    http://localhost:8000/admin

On CLIENT devices connected to the SAME Wi-Fi/LAN, open:
    http://<host-machine-LAN-IP>:8000/scan

(Find the host's LAN IP with `ipconfig` on Windows or `ifconfig` / `ip a`
on macOS/Linux — look for something like 192.168.x.x)
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

# Serve any static assets (none required by default, but ready for use)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---------------------------------------------------------------------------
# WEBSOCKET CONNECTION MANAGER
# ---------------------------------------------------------------------------
# The /admin page keeps one WebSocket open to receive two kinds of live
# pushes from the server:
#   1. {"type": "qr", ...}          -> every 10s, a fresh QR code + countdown
#   2. {"type": "attendance", ...}  -> whenever the attendance table changes
# This avoids the admin page having to poll the server repeatedly.
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
        # Iterate over a copy since a failed send may trigger disconnect
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
    Generates the current time-hashed token and renders it as a QR code
    image encoded as a base64 data URI, ready to drop into an <img> tag.
    """
    token_info = security.generate_current_token()

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    # The QR code's payload is JUST the token string. The scanning client
    # reads this token and sends it back to /api/checkin along with the
    # user's name and ID for validation.
    qr.add_data(token_info["token"])
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")

    return {
        "type": "qr",
        "qr_image": f"data:image/png;base64,{encoded}",
        "expires_in": token_info["expires_in"],
        "step_seconds": token_info["step_seconds"],
    }


# ---------------------------------------------------------------------------
# BACKGROUND TASK: rotate + broadcast the QR code every TIME_STEP seconds
# ---------------------------------------------------------------------------
async def qr_rotation_loop():
    while True:
        payload = build_qr_payload()
        await manager.broadcast_json(payload)
        # Sleep until the *next* time-step boundary so rotation stays
        # aligned with the server's validation window, rather than drifting.
        await asyncio.sleep(payload["expires_in"])


@app.on_event("startup")
async def on_startup():
    # Initialize the local SQLite database (creates the file if missing)
    database.init_db()
    # Load (or generate) the persisted HMAC secret used for token signing
    security.set_secret_key(database.get_or_create_secret_key())
    # Kick off the recurring QR rotation/broadcast loop in the background.
    # Stored on app.state so the task isn't garbage-collected mid-flight.
    app.state.qr_task = asyncio.create_task(qr_rotation_loop())


async def broadcast_attendance_update():
    """Pushes the full current attendance list to all connected admin clients."""
    records = database.fetch_all_records()
    await manager.broadcast_json({"type": "attendance", "records": records})


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
    records = database.fetch_all_records()
    return templates.TemplateResponse(
        "admin.html", {"request": request, "records": records}
    )


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
        # On connect, immediately send current state so the page doesn't
        # have to wait up to 10s for the next rotation to see a QR code.
        await websocket.send_json(build_qr_payload())
        await websocket.send_json(
            {"type": "attendance", "records": database.fetch_all_records()}
        )
        while True:
            # We don't expect messages from the admin client, but keep the
            # loop alive to detect disconnects.
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
    device_id: str = ""  # random per-browser ID from localStorage, see scan.html


class ManualAddRequest(BaseModel):
    full_name: str
    id_number: str
    status: str = "Absent"  # "Absent" or "Present"


class StatusUpdateRequest(BaseModel):
    status: str  # "Absent" or "Present"


# ---------------------------------------------------------------------------
# API: CLIENT CHECK-IN (token-validated)
# ---------------------------------------------------------------------------
@app.post("/api/checkin")
async def checkin(payload: CheckinRequest, request: Request):
    full_name = payload.full_name.strip()
    id_number = payload.id_number.strip()
    device_id = payload.device_id.strip()
    # request.client.host is the submitting device's LAN IP, as seen by the
    # server. On a normal local Wi-Fi network each device gets its own IP
    # from the router, so this reliably identifies "which phone" submitted —
    # unlike the token, which many people legitimately scan at once.
    source_ip = request.client.host if request.client else ""

    if not full_name or not id_number:
        raise HTTPException(status_code=400, detail="Name and ID are required.")

    # --- ANTI-CHEAT CHECK #1: is the QR token itself currently valid? ---
    if not security.validate_token(payload.token):
        return {
            "success": False,
            "message": "QR code expired or invalid. Please scan the current code on screen and try again.",
        }

    # --- ANTI-CHEAT CHECK #2: has this device already checked in someone else? ---
    # This is what stops one phone from scanning once, then typing in a
    # friend's name and submitting again. It does NOT stop the same person
    # re-submitting (e.g. a page refresh) — only a DIFFERENT id_number from
    # the same device/IP is blocked.
    with database.get_connection() as conn:
        conflict = conn.execute(
            """SELECT id_number FROM attendance
               WHERE status = 'Present' AND id_number != ?
                 AND ((device_id != '' AND device_id = ?) OR (source_ip != '' AND source_ip = ?))
               LIMIT 1""",
            (id_number, device_id, source_ip),
        ).fetchone()

    if conflict:
        return {
            "success": False,
            "message": "This device has already checked someone else in. Each device can only check in one person — please ask a staff member if you need help.",
        }

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with database.get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM attendance WHERE id_number = ?", (id_number,)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE attendance
                   SET full_name = ?, status = 'Present', timestamp = ?, method = 'QR Scan',
                       source_ip = ?, device_id = ?
                   WHERE id_number = ?""",
                (full_name, now, source_ip, device_id, id_number),
            )
        else:
            conn.execute(
                """INSERT INTO attendance
                   (full_name, id_number, status, timestamp, method, source_ip, device_id)
                   VALUES (?, ?, 'Present', ?, 'QR Scan', ?, ?)""",
                (full_name, id_number, now, source_ip, device_id),
            )
        conn.commit()

    await broadcast_attendance_update()

    return {
        "success": True,
        "message": f"Welcome, {full_name}! You're checked in at {now}.",
    }


# ---------------------------------------------------------------------------
# API: ADMIN CRUD OPERATIONS
# ---------------------------------------------------------------------------
@app.post("/api/manual_add")
async def manual_add(payload: ManualAddRequest):
    full_name = payload.full_name.strip()
    id_number = payload.id_number.strip()
    status = payload.status if payload.status in ("Absent", "Present") else "Absent"

    if not full_name or not id_number:
        raise HTTPException(status_code=400, detail="Name and ID are required.")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status == "Present" else None

    with database.get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM attendance WHERE id_number = ?", (id_number,)
        ).fetchone()
        if existing:
            raise HTTPException(
                status_code=409, detail="A record with this ID number already exists."
            )
        conn.execute(
            """INSERT INTO attendance (full_name, id_number, status, timestamp, method)
               VALUES (?, ?, ?, ?, 'Manual')""",
            (full_name, id_number, status, timestamp),
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
# API: CSV EXPORT
# ---------------------------------------------------------------------------
@app.get("/api/export_csv")
async def export_csv():
    records = database.fetch_all_records()

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

    filename = f"attendance_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    import uvicorn

    # host="0.0.0.0" is essential — it makes the server reachable from
    # other devices on the LAN, not just the host machine itself.
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
