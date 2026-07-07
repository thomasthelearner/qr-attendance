#!/bin/bash
# One command to start the server with HTTPS every time.
# Assumes mkcert has already generated the .pem files in this folder
# (see README's "Local HTTPS" section) — this script just finds them
# automatically so you never have to retype exact filenames.

set -e
cd "$(dirname "$0")"

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

source venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000 \
  --ssl-certfile "$CERT_FILE" \
  --ssl-keyfile "$KEY_FILE"
