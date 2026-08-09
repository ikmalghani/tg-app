#!/bin/sh
set -e

persist_session() {
  if [ -f /app/tg-upload/profile.session ]; then
    cp -f /app/tg-upload/profile.session /data/profile.session
  fi
  if [ -f /app/tg-upload/profile.session-journal ]; then
    cp -f /app/tg-upload/profile.session-journal /data/profile.session-journal
  fi
}

# Restore Telegram session from the data volume (survives image rebuilds)
if [ -f /data/profile.session ]; then
  cp -f /data/profile.session /app/tg-upload/profile.session
fi
if [ -f /data/profile.session-journal ]; then
  cp -f /data/profile.session-journal /app/tg-upload/profile.session-journal
fi

# Periodic sync while Authorize / long jobs may write the session
(
  while true; do
    sleep 60
    persist_session
  done
) &
SYNC_PID=$!

uvicorn app.main:app --host 0.0.0.0 --port 8000 &
APP_PID=$!

shutdown() {
  kill "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
  kill "$SYNC_PID" 2>/dev/null || true
  persist_session
  exit 0
}
trap shutdown INT TERM

wait "$APP_PID"
STATUS=$?
kill "$SYNC_PID" 2>/dev/null || true
persist_session
exit "$STATUS"
