#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  start.sh  —  launch voice_paste in the background
#  Log output goes to voice_paste.log in the same folder
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$SCRIPT_DIR/voice_paste.log"
PID_FILE="$SCRIPT_DIR/voice_paste.pid"

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
nohup python3 "$SCRIPT_DIR/voice_paste.py" > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

echo ""
echo "  ✓  voice_paste is running (PID $PID)"
echo "  Hold Ctrl+Space to record, release to paste."
echo ""
echo "  Logs:  tail -f $LOG"
echo "  Stop:  kill \$(cat $PID_FILE)"
echo ""
