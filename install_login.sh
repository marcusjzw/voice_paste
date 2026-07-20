#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  install_login.sh  —  auto-start voice_paste on every login.
#
#  NOTE: This used to install a bare `python3 voice_paste.py` LaunchAgent, but
#  that gives the process no app identity — so macOS never shows the Microphone
#  prompt and mic access stays denied. The mic + autostart setup now lives in
#  install_app.sh, which packages a real VoicePaste.app (correct TCC identity,
#  double-clickable) and installs the LaunchAgent that launches it.
#
#  This script now just delegates there so old muscle memory keeps working.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "install_login.sh → delegating to install_app.sh (app-bundle install)…"
exec bash "$SCRIPT_DIR/install_app.sh"
