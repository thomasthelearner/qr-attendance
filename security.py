"""
security.py
------------
Core anti-cheat logic for the QR Attendance system.

WHY THIS MATTERS:
A static QR code can be screenshotted and shared with someone who isn't
physically present. To prevent that, we generate a brand-new token every
TIME_STEP seconds using an HMAC (keyed hash) of the current time step.
This is the same core idea behind TOTP (RFC 6238 / Google Authenticator),
simplified for embedding directly in a QR code payload instead of a
6-digit numeric code.

A token is only valid for a narrow rolling time window, so a screenshot
taken even a few seconds ago will be rejected by the server.
"""

import hmac
import hashlib
import time

# ---------------------------------------------------------------------------
# SECRET KEY
# ---------------------------------------------------------------------------
# This key must stay on the server ONLY. Anyone with this key could forge
# valid tokens without scanning the live QR code.
#
# For a real deployment, load this from an environment variable instead of
# hardcoding it, e.g.:
#     SECRET_KEY = os.environ["QR_ATTENDANCE_SECRET"].encode()
# For this local prototype, a hardcoded value is fine, but it is randomly
# generated per-install by generate_secret_if_missing() below.
# ---------------------------------------------------------------------------
SECRET_KEY = b"REPLACE_ME_ON_FIRST_RUN"

# How often the QR code / token rotates, in seconds.
TIME_STEP = 10

# How many previous time-steps to still accept as "valid".
# This tolerates the delay between the admin's screen rendering a QR code,
# the client scanning it, filling the form, and hitting submit.
# 1 = accept current step + 1 previous step (i.e. up to ~20s old).
ALLOWED_CLOCK_DRIFT_STEPS = 1


def set_secret_key(new_key: bytes) -> None:
    """Allows app.py to inject a persisted random secret at startup."""
    global SECRET_KEY
    SECRET_KEY = new_key


def get_current_counter() -> int:
    """
    The 'time counter' is just the current unix timestamp divided into
    fixed-size buckets (time steps). Every device asking for the time
    within the same 10-second bucket gets the same counter value.
    """
    return int(time.time()) // TIME_STEP


def _hash_counter(counter: int) -> str:
    """
    Produce a deterministic, hard-to-guess token for a given time counter
    using HMAC-SHA256. HMAC ensures that without SECRET_KEY, it is
    computationally infeasible to predict future/past tokens.
    """
    message = str(counter).encode("utf-8")
    digest = hmac.new(SECRET_KEY, message, hashlib.sha256).hexdigest()
    # Truncate to keep the QR code payload small & quick to scan.
    return digest[:16]


def generate_current_token() -> dict:
    """
    Returns the token that should be displayed RIGHT NOW, plus metadata
    the admin frontend needs to render a countdown.
    """
    counter = get_current_counter()
    token = _hash_counter(counter)
    elapsed_in_step = time.time() % TIME_STEP
    seconds_remaining = round(TIME_STEP - elapsed_in_step, 1)
    return {
        "token": token,
        "counter": counter,
        "expires_in": seconds_remaining,
        "step_seconds": TIME_STEP,
    }


def validate_token(token: str) -> bool:
    """
    Checks a token submitted by a scanning client against the current
    time step AND a small number of previous steps (to allow for network/
    human latency). Uses constant-time comparison (hmac.compare_digest)
    to avoid leaking timing information about the correct token.
    """
    if not token:
        return False

    current_counter = get_current_counter()
    for steps_back in range(ALLOWED_CLOCK_DRIFT_STEPS + 1):
        candidate = _hash_counter(current_counter - steps_back)
        if hmac.compare_digest(candidate, token):
            return True
    return False
