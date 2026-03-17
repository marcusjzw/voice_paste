#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  install_login.sh  —  auto-start voice_paste on every login via LaunchAgent
#  Run once. To uninstall, run:  launchctl unload ~/Library/LaunchAgents/com.voicepaste.plist
# ─────────────────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(which python3)"
PLIST="$HOME/Library/LaunchAgents/com.voicepaste.plist"

echo ""
echo "══════════════════════════════════════════════"
echo "   voice_paste  login auto-start setup"
echo "══════════════════════════════════════════════"
echo ""
echo "  Python:  $PYTHON"
echo "  Script:  $SCRIPT_DIR/voice_paste.py"
echo "  Plist:   $PLIST"
echo ""

# ── Unload existing if present ────────────────────────────────────────────
if [ -f "$PLIST" ]; then
  echo "  Removing existing LaunchAgent…"
  launchctl unload "$PLIST" 2>/dev/null || true
fi

# ── Write plist ───────────────────────────────────────────────────────────
cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.voicepaste</string>

  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$SCRIPT_DIR/voice_paste.py</string>
  </array>

  <!-- Start immediately when loaded, and restart if it crashes -->
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>

  <!-- Log output -->
  <key>StandardOutPath</key>
  <string>$SCRIPT_DIR/voice_paste.log</string>
  <key>StandardErrorPath</key>
  <string>$SCRIPT_DIR/voice_paste.log</string>

  <!-- Ensure Homebrew and system Python paths are available -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

# ── Load it ───────────────────────────────────────────────────────────────
launchctl load "$PLIST"

echo "  ✓  LaunchAgent installed and started"
echo ""
echo "  voice_paste will now launch automatically on every login."
echo ""
echo "  To check it's running:  launchctl list | grep voicepaste"
echo "  To stop auto-start:     launchctl unload $PLIST"
echo "  To remove entirely:     launchctl unload $PLIST && rm $PLIST"
echo ""
