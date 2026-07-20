#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  install_app.sh  —  package voice_paste as a double-clickable macOS .app and
#                     install a LaunchAgent that autostarts it on login.
#
#  Why the nested Python.app?
#    macOS TCC attributes Microphone / Accessibility to the *bundle that owns
#    the running executable*. The Python.framework interpreter always reports
#    its identity as the framework's own "Python.app" (org.python.python),
#    which has no NSMicrophoneUsageDescription — so the mic prompt can never
#    appear and access is silently denied. We fix this by copying the tiny
#    Python.app launcher stub *into* our bundle and rebranding it as
#    "VoicePaste" (com.voicepaste) with a mic usage string. The interpreter
#    then runs as VoicePaste, the prompt appears, and VoicePaste shows up in
#    System Settings → Privacy & Security. No system files are modified.
#
#  Re-runnable: rebuilds the bundle and reloads the agent cleanly.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/Applications/VoicePaste.app"
PLIST="$HOME/Library/LaunchAgents/com.voicepaste.plist"
LABEL="com.voicepaste"
LOG="$HOME/Library/Logs/voice_paste.log"

# Framework python that has the deps installed. The nested Python.app stub is
# copied from this framework version.
PYVER="3.14"
FRAMEWORK="/Library/Frameworks/Python.framework/Versions/$PYVER"
SRC_PYTHON_APP="$FRAMEWORK/Resources/Python.app"
INNER_APP="$APP_DIR/Contents/Resources/Python.app"
INNER_PY="$INNER_APP/Contents/MacOS/Python"

[ -d "$SRC_PYTHON_APP" ] || { echo "ERROR: $SRC_PYTHON_APP not found"; exit 1; }

ARCH_FLAG=""
[ "$(uname -m)" = "arm64" ] && ARCH_FLAG="arch -arm64"

echo ""
echo "══════════════════════════════════════════════"
echo "   VoicePaste  →  app bundle + autostart"
echo "══════════════════════════════════════════════"
echo "  Repo:    $REPO_DIR"
echo "  App:     $APP_DIR"
echo "  Runtime: $SRC_PYTHON_APP  (copied in)"
echo ""

# ── 1. Stop any running instance ─────────────────────────────────────────────
echo "▸ Stopping any running instance…"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
/usr/bin/pkill -f "$REPO_DIR/voice_paste.py" 2>/dev/null || true
sleep 1

# ── 2. Build the .app bundle ─────────────────────────────────────────────────
echo "▸ Building ${APP_DIR}…"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

# 2a. Copy the Python.app launcher stub into the bundle and rebrand it. This
#     copied stub still loads the shared framework (interpreter + site-packages)
#     but now reports itself as VoicePaste, which is what TCC keys on.
echo "  • embedding Python.app runtime as VoicePaste…"
cp -R "$SRC_PYTHON_APP" "$INNER_APP"
INNER_PLIST="$INNER_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.voicepaste" "$INNER_PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleName VoicePaste" "$INNER_PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleName string VoicePaste" "$INNER_PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string VoicePaste" "$INNER_PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$INNER_PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string VoicePaste records your voice while you hold the hotkey, then transcribes it to text." "$INNER_PLIST" 2>/dev/null || true

# 2b. Launcher: the outer bundle's main executable. Runs the repo script with
#     the *embedded* interpreter so the process identity is VoicePaste.
cat > "$APP_DIR/Contents/MacOS/VoicePaste" << EOF
#!/bin/bash
# Route the app's own output to the log (launchd only captures 'open', not us).
exec >> "$LOG" 2>&1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
echo "[launcher] \$(date '+%Y-%m-%d %H:%M:%S') starting VoicePaste (pid \$\$)"
# arch pin: launched via LaunchServices from a Rosetta context the app would
# otherwise inherit x86_64 and fail to load the arm64-only numpy/scipy wheels.
exec $ARCH_FLAG "$INNER_PY" -u "$REPO_DIR/voice_paste.py"
EOF
chmod +x "$APP_DIR/Contents/MacOS/VoicePaste"

# 2c. Outer bundle Info.plist — the double-clickable wrapper. Distinct id from
#     the inner runtime to avoid duplicate-bundle-id confusion in LaunchServices.
cat > "$APP_DIR/Contents/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>VoicePaste</string>
  <key>CFBundleDisplayName</key>       <string>VoicePaste</string>
  <key>CFBundleIdentifier</key>        <string>com.voicepaste.launcher</string>
  <key>CFBundleExecutable</key>        <string>VoicePaste</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key> <string>6.0</string>
  <key>CFBundleShortVersionString</key> <string>1.5.0</string>
  <key>CFBundleVersion</key>           <string>1.5.0</string>
  <key>LSMinimumSystemVersion</key>    <string>10.15</string>
  <key>LSUIElement</key>               <true/>
  <!-- Required in the LAUNCHED (outer) bundle too: TCC checks the app that
       LaunchServices launched, and crashes the process if the key is absent
       when it touches the microphone. -->
  <key>NSMicrophoneUsageDescription</key>
  <string>VoicePaste records your voice while you hold the hotkey, then transcribes it to text.</string>
</dict>
</plist>
EOF

printf 'APPL????' > "$APP_DIR/Contents/PkgInfo"

# ── 3. Ad-hoc code sign (inner first, then the outer bundle) ─────────────────
echo "▸ Code signing (ad-hoc)…"
codesign --force --sign - "$INNER_APP" >/dev/null 2>&1 || echo "  (inner sign skipped)"
codesign --force --sign - "$APP_DIR"   >/dev/null 2>&1 || echo "  (outer sign skipped)"
codesign --verify --deep "$APP_DIR"    >/dev/null 2>&1 && echo "  signed ✓" || echo "  (unverified — continuing)"

# Register with LaunchServices so Spotlight/Finder see it immediately.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP_DIR" 2>/dev/null || true

# ── 4. Install the LaunchAgent (autostart + auto-restart) ────────────────────
# We exec the bundle's launcher DIRECTLY rather than via `open -a`. Launching
# the outer bundle through LaunchServices (which then execs the inner
# Python.app) confuses window-server app registration and the menu-bar status
# item never appears. A direct exec in the gui/<uid> domain runs it as a normal
# menu-bar agent — the icon shows, launchd owns the process (clean KeepAlive /
# bootout), and the mic grant already persists on the com.voicepaste identity
# so no LaunchServices launch is needed for the prompt anymore.
echo "▸ Installing LaunchAgent…"
cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_DIR/Contents/MacOS/VoicePaste</string>
  </array>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>ProcessType</key>      <string>Interactive</string>
  <key>StandardOutPath</key>  <string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

: > "$LOG"
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo ""
echo "══════════════════════════════════════════════"
echo "  ✓  VoicePaste installed"
echo ""
echo "  • Launch from Finder/Spotlight:  VoicePaste"
echo "  • Autostarts on every login (menu bar 🎙)"
echo "  • Logs:   tail -f $LOG"
echo "  • Stop:   launchctl bootout gui/\$(id -u)/$LABEL"
echo ""
echo "  On first launch macOS asks for Microphone +"
echo "  Accessibility access — click Allow (shown as"
echo "  \"VoicePaste\")."
echo "══════════════════════════════════════════════"
echo ""
