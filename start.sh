#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  start.sh  —  launch voice_paste in the background
#  Log output goes to voice_paste.log in the same folder
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$SCRIPT_DIR/voice_paste.log"
PID_FILE="$SCRIPT_DIR/voice_paste.pid"

# ── If launchd is managing the daemon, bounce it through launchctl ────────
# Otherwise we end up with two processes (this nohup + launchd's KeepAlive
# respawn) both grabbing the mic, which spams the error chime.
if launchctl list 2>/dev/null | grep -q '^[^[:space:]]*[[:space:]][^[:space:]]*[[:space:]]com\.voicepaste$'; then
  echo "LaunchAgent com.voicepaste is loaded — restarting via launchctl…"
  launchctl kickstart -k "gui/$(id -u)/com.voicepaste"
  rm -f "$PID_FILE"
  echo ""
  echo "  ✓  voice_paste restarted via launchctl"
  echo "  Logs:  tail -f $LOG"
  echo ""
  exit 0
fi

# ── Stop any existing instance ────────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Stopping existing voice_paste (PID $OLD_PID)…"
    kill "$OLD_PID"
    sleep 0.5
  fi
  rm -f "$PID_FILE"
fi

# ── Launch ────────────────────────────────────────────────────────────────
echo "Starting voice_paste…"
nohup python3 -u "$SCRIPT_DIR/voice_paste.py" > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

echo ""
echo "  ✓  voice_paste is running (PID $PID)"
echo "  Hold Ctrl+Space to record, release to paste."
echo ""
echo "  Logs:  tail -f $LOG"
echo "  Stop:  kill \$(cat $PID_FILE)"
echo ""
