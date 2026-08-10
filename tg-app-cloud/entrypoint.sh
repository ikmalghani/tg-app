#!/bin/sh
set -e

DATA_DIR="${DATA_DIR:-/data}"
TG_UPLOAD_DIR="${TG_UPLOAD_DIR:-/app/tg-upload}"
CRYPT_CONFIG="${CRYPT_CONFIG:-/data/crypt.conf}"

mkdir -p "$DATA_DIR/uploads" "$DATA_DIR/downloads" "$DATA_DIR/jobs"

# Do NOT seed an empty crypt.conf.example — that breaks decrypt (blank passwords).
# User must upload/paste a real crypt.conf via the UI (saved to $DATA_DIR/crypt.conf).

persist_session() {
  if [ -f "$TG_UPLOAD_DIR/profile.session" ]; then
    cp -f "$TG_UPLOAD_DIR/profile.session" "$DATA_DIR/profile.session"
  fi
  if [ -f "$TG_UPLOAD_DIR/profile.session-journal" ]; then
    cp -f "$TG_UPLOAD_DIR/profile.session-journal" "$DATA_DIR/profile.session-journal"
  fi
}

# Restore Telegram session from the data volume (survives image rebuilds)
if [ -f "$DATA_DIR/profile.session" ]; then
  cp -f "$DATA_DIR/profile.session" "$TG_UPLOAD_DIR/profile.session"
fi
if [ -f "$DATA_DIR/profile.session-journal" ]; then
  cp -f "$DATA_DIR/profile.session-journal" "$TG_UPLOAD_DIR/profile.session-journal"
fi

# NAS bind mounts are often root-owned; app runs as uid 1000
if [ "$(id -u)" = "0" ]; then
  chown -R appuser:appuser "$DATA_DIR" "$TG_UPLOAD_DIR" || true
fi

# Periodic sync while Authorize / long jobs may write the session
(
  while true; do
    sleep 5
    persist_session
  done
) &
SYNC_PID=$!

shutdown() {
  kill "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
  kill "$SYNC_PID" 2>/dev/null || true
  persist_session
  exit 0
}
trap shutdown INT TERM

if [ "$(id -u)" = "0" ]; then
  runuser -u appuser -- uvicorn app.main:app --host 0.0.0.0 --port 8000 &
else
  uvicorn app.main:app --host 0.0.0.0 --port 8000 &
fi
APP_PID=$!

wait "$APP_PID"
STATUS=$?
kill "$SYNC_PID" 2>/dev/null || true
persist_session
exit "$STATUS"
