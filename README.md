# Checkpoint — Local-First QR Attendance System

A self-contained attendance system that runs entirely on one machine (the
"host") and is used by attendees' phones over the same local Wi-Fi/LAN.
No cloud services, no external database — everything lives in one SQLite
file.

## How the anti-cheat works

The `/admin` page displays a QR code that encodes a **token**, not a fixed
ID. The token is an HMAC-SHA256 hash of the current 10-second time bucket,
keyed with a secret that never leaves the server (`security.py`). Every
10 seconds the bucket changes, so the token — and therefore the QR code —
changes too. When a client scans it and submits, the server recomputes
the expected token for the current (and immediately preceding) time bucket
and checks for a match. A screenshot of the QR code is only useful for
about 10–20 seconds, and only from wherever it's actually being displayed
in real time.

## Project structure

```
qr-attendance/
├── app.py                # FastAPI app: routes, WebSocket, QR + CRUD endpoints
├── security.py            # Time-hashed token generation & validation
├── database.py            # SQLite schema + connection helpers
├── requirements.txt
├── templates/
│   ├── admin.html          # Admin panel (QR display + live ledger)
│   └── scan.html            # Mobile check-in page (camera QR scan + form)
├── static/                 # (reserved for any extra assets)
├── exports/                 # CSV exports land here if you save them locally
├── attendance.db            # created automatically on first run
└── .secret_key               # created automatically on first run — do not share
```

## Setup

```bash
cd qr-attendance
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` is what makes the server reachable from other devices on
the LAN — not just `localhost` on the host machine.

- **Host machine (admin):** open `http://localhost:8000/admin`
- **Attendee phones:** find the host's LAN IP address and open
  `http://<host-LAN-IP>:8000/scan`

Find your LAN IP:
- macOS/Linux: `ifconfig` or `ip a` (look for something like `192.168.x.x`)
- Windows: `ipconfig` (look for "IPv4 Address" under your Wi-Fi adapter)

Make sure the host machine's firewall allows inbound connections on port
8000, and that all devices are on the **same** Wi-Fi network/subnet.

## ⚠️ Important: camera access requires a "secure origin"

Browsers only allow camera access (`getUserMedia`, used by `/scan` to read
the QR code) on `https://` origins or `localhost` — plain `http://192.168.x.x`
is treated as insecure and the camera prompt will be blocked. You have two
easy options for a local demo:

1. **Chrome flag (fastest for testing):** on each phone, go to
   `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, add
   `http://<host-LAN-IP>:8000`, enable it, and relaunch Chrome.
2. **Local HTTPS (better for a real event):** generate a self-signed cert
   with [mkcert](https://github.com/FiloSottile/mkcert) and run uvicorn with
   `--ssl-keyfile` / `--ssl-certfile`. Phones will need to trust the cert
   once (mkcert has a one-line install for this).

The manual token entry field on `/scan` ("Camera not working? Enter code
manually") lets you test the check-in flow without a camera at all — just
type the token currently shown under the QR code on `/admin` (open the
browser dev tools / inspect element, or briefly add a debug label — the
token isn't shown on-screen by default since displaying it as plain text
defeats the purpose of the QR code for real use).

## Stopping one device from checking in multiple people

The QR token is deliberately shareable by design — many people scan the
*same* code at once, and that's normal. What's not okay is one phone
checking itself in, then typing a friend's name and submitting again.

To stop that, every check-in records:
- **The device's LAN IP address** (seen directly by the server — not
  something the browser can fake or clear).
- **A random device ID** stored in the browser's `localStorage`, so the
  same phone is recognized across page reloads.

If a device/IP that already has a successful check-in tries to submit a
**different** person, the request is rejected with a clear message. The
same person re-submitting (e.g. a page refresh) is still allowed.

The admin ledger has a "Source" column, and any row sharing a device/IP
with a different checked-in person gets a `⚠ shared device` flag so you
can spot it at a glance — useful for reviewing anything that happened
before this feature existed, or investigating an edge case in person.

**Honest limitation:** this stops the casual case (one phone, a few
names typed in by hand). It does not stop someone who deliberately
switches to mobile data, uses a VPN, or opens a private/incognito window
to get a fresh identity — there's no fully bulletproof way to do that
without requiring real accounts/login. For a low-stakes local event this
is normally enough; for anything higher-stakes, pair it with an admin
glancing at the live ledger as people walk in.

## Features

- **Live rotating QR code** — refreshes every 10 seconds over a WebSocket,
  countdown ring shows time remaining.
- **Live attendance ledger** — updates instantly for all connected admins
  whenever anyone checks in or a record is edited, no page refresh needed.
- **Manual CRUD** — "Manually Add Record" for walk-ins, one-click
  Present/Absent toggle per row, and delete.
- **CSV export** — `Export CSV` button streams a timestamped `.csv` of the
  full table.
- **Mobile check-in page** — camera-based QR scan + name/ID form, clear
  success/failure banner.

## Production hardening ideas (not included in this prototype)

This is intentionally a minimal prototype. Before using it for anything
beyond a local trial, consider:

- **Admin auth** — `/admin` and its API routes currently have no login.
  Add HTTP Basic Auth or a session cookie so only staff can view the
  roster or edit records.
- **Rate limiting** on `/api/checkin` to blunt brute-force token guessing.
- **HTTPS by default** (see above) rather than an opt-in flag.
- **Input validation / sanitization** hardening beyond the basics here.
- **Duplicate-device detection** if you want to prevent one phone from
  checking in multiple different people back-to-back.
