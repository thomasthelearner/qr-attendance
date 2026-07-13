#!/bin/bash
# One command to start the server with HTTPS every time.
# Assumes mkcert has already generated the .pem files in this folder
# (see README's "Local HTTPS" section) — this script finds them
# automatically so you never have to retype exact filenames.

set -e
cd "$(dirname "$0")"

# Self-heal: if there's no venv, or it's broken (e.g. its Python got
# removed/replaced by a Homebrew upgrade), rebuild it from scratch
# instead of failing with a confusing "command not found" error.
if [ ! -x "venv/bin/python3" ]; then
  echo "⚙️  Setting up a fresh virtual environment (one-time)…"
  rm -rf venv
  python3 -m venv venv
fi

# Self-heal: if the venv exists but dependencies were never installed
# (fresh venv, or one that got rebuilt above), install them now.
if [ ! -f "venv/bin/uvicorn" ]; then
  echo "⚙️  Installing dependencies (one-time)…"
  venv/bin/pip install -r requirements.txt
  echo ""
fi

CERT_FILE=$(ls *.local+*.pem 2>/dev/null | grep -v -- '-key.pem' | head -n 1)
KEY_FILE=$(ls *.local+*-key.pem 2>/dev/null | head -n 1)

if [ -z "$CERT_FILE" ] || [ -z "$KEY_FILE" ]; then
  echo "❌ Couldn't find mkcert certificate files (*.pem) in this folder."
  echo "   Run the mkcert setup from the README first, then try again."
  exit 1
fi

echo "✅ Using certificate: $CERT_FILE"
echo "✅ Using key:         $KEY_FILE"
echo ""

# Calling venv/bin/uvicorn directly (instead of "source venv/bin/activate"
# first) avoids any issues with the venv not being properly on PATH.
venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 \
  --ssl-certfile "$CERT_FILE" \
  --ssl-keyfile "$KEY_FILE"
