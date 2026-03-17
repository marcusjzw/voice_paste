#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  voice_paste  —  one-time setup script (macOS)
# ─────────────────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

echo ""
echo "══════════════════════════════════════════════"
echo "   voice_paste  setup"
echo "══════════════════════════════════════════════"
echo ""

# ── 1. Python 3.9+ ────────────────────────────────────────────────────────
echo "▸ Checking Python version…"
python3 -c "import sys; assert sys.version_info >= (3,9), 'Python 3.9+ required'" \
  || { echo "ERROR: Python 3.9 or higher is required."; exit 1; }
python3 --version
echo ""

# ── 2. Homebrew + PortAudio (sounddevice dependency) ─────────────────────
echo "▸ Checking PortAudio (needed for microphone access)…"
if ! command -v brew &>/dev/null; then
  echo "  Homebrew not found. Installing…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

if ! brew list portaudio &>/dev/null 2>&1; then
  echo "  Installing portaudio via Homebrew…"
  brew install portaudio
else
  echo "  PortAudio already installed ✓"
fi
echo ""

# ── 3. Python packages ────────────────────────────────────────────────────
echo "▸ Installing Python packages…"
pip3 install --upgrade pip --quiet
pip3 install -r "$SCRIPT_DIR/requirements.txt"
echo ""

# ── 4. API key ────────────────────────────────────────────────────────────
echo "▸ OpenAI API key setup…"
if [ -f "$ENV_FILE" ] && grep -q "OPENAI_API_KEY=sk-" "$ENV_FILE" 2>/dev/null; then
  echo "  .env already contains an API key ✓"
  echo "  (To update it, edit $ENV_FILE directly)"
else
  echo ""
  echo "  Your API key will be saved to: $ENV_FILE"
  echo "  (This file stays local — never commit it to git)"
  echo ""
  read -r -p "  Paste your OpenAI API key (sk-…): " key
  if [[ "$key" != sk-* ]]; then
    echo "  WARNING: Key doesn't start with 'sk-' — double-check it's correct."
  fi
  echo "OPENAI_API_KEY=$key" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"   # owner read-only
  echo "  Saved ✓"
fi
echo ""

# ── 5. Free up Ctrl+Space shortcut ───────────────────────────────────────
echo "══════════════════════════════════════════════"
echo "  ⚠  IMPORTANT: Free up Ctrl+Space on macOS"
echo "══════════════════════════════════════════════"
echo ""
echo "  macOS uses Ctrl+Space to switch input sources."
echo "  You need to disable it, or voice_paste won't"
echo "  capture the shortcut reliably."
echo ""
echo "  Steps:"
echo "  1. Open: System Settings → Keyboard"
echo "  2. Click: Keyboard Shortcuts → Input Sources"
echo "  3. Uncheck: 'Select the previous input source' (Ctrl+Space)"
echo ""
echo "  Press any key to open Keyboard Shortcuts now…"
read -r -n 1 -s
open "x-apple.systempreferences:com.apple.preference.keyboard?Shortcuts"
echo ""

# ── 6. Accessibility permission reminder ─────────────────────────────────
echo "══════════════════════════════════════════════"
echo "  ⚠  IMPORTANT: Grant Accessibility access"
echo "══════════════════════════════════════════════"
echo ""
echo "  voice_paste needs Accessibility permission to"
echo "  detect global keyboard shortcuts."
echo ""
echo "  Steps:"
echo "  1. System Settings → Privacy & Security → Accessibility"
echo "  2. Add Terminal (or your terminal app) to the list"
echo "  3. Toggle it ON"
echo ""
echo "  Press any key to open Privacy & Security now…"
read -r -n 1 -s
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
echo ""

echo "══════════════════════════════════════════════"
echo "  ✓  Setup complete!"
echo ""
echo "  To start:  bash start.sh"
echo "  Or:        python3 voice_paste.py"
echo "══════════════════════════════════════════════"
echo ""
