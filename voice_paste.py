#!/usr/bin/env python3
"""
voice_paste.py
──────────────
Hold Ctrl+Space to record. Release to transcribe via OpenAI Whisper and
paste the result at your cursor — on any app, any surface.

Menu bar: 🎙 idle  |  🔴 0s / 1s / 2s… while recording  |  ⠸ spinner while transcribing
Mic selection available from the menu bar icon.

Requirements: see requirements.txt
Setup:        run setup.sh first
"""

import os
import sys
import time
import threading
import tempfile
import subprocess
import pathlib

import numpy as np
import sounddevice as sd
import scipy.io.wavfile as wavfile
from pynput import keyboard
from openai import OpenAI
import rumps

# ── Version ──────────────────────────────────────────────────────────────────
_VERSION_FILE = pathlib.Path(__file__).parent / "VERSION"
VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "unknown"

# ── Load .env (always overrides shell env) ───────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(
        dotenv_path=os.path.join(os.path.dirname(__file__), ".env"),
        override=True,
    )
except ImportError:
    pass

# ── Config ───────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16_000   # Hz — Whisper is optimised for 16 kHz
CHANNELS    = 1
MODEL       = "gpt-4o-transcribe"
LANGUAGE    = "en"     # Set to None for automatic language detection
API_KEY     = os.environ.get("OPENAI_API_KEY", "")

if not API_KEY:
    print(
        "\n[voice_paste] ERROR: OPENAI_API_KEY is not set.\n"
        "  Add  OPENAI_API_KEY=sk-...  to a .env file next to this script.\n"
    )
    sys.exit(1)

client = OpenAI(api_key=API_KEY)

# ── Shared state ─────────────────────────────────────────────────────────────
_recording        = False
_transcribing     = False
_ctrl_held        = False
_space_held       = False
_frames: list[np.ndarray] = []
_rec_thread: threading.Thread | None = None
_state_lock       = threading.Lock()
_recording_start  = 0.0        # time.time() when recording began
_selected_device  = None       # None = system default; int = device index
_app              = None       # set after rumps app is created
_REPO_DIR         = pathlib.Path(__file__).parent

_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_spinner_idx = 0

# ── Stop chime (plays on release via NSSound on the main thread) ──────────────
try:
    import AppKit as _AppKit
    from Foundation import NSObject as _NSObject

    class _SoundDispatcher(_NSObject):
        """Thin NSObject so we can use performSelectorOnMainThread for instant dispatch."""
        def playSound_(self, sound):
            sound.stop()
            sound.play()

    _dispatcher = _SoundDispatcher.new()
    _STOP_SOUND = _AppKit.NSSound.soundNamed_("Pop").copy()
    _STOP_SOUND.setVolume_(0.5)
except Exception as _e:
    print(f"[voice_paste] Could not load stop chime: {_e}", flush=True)
    _dispatcher = _STOP_SOUND = None

def _play_stop_chime() -> None:
    """Dispatch Pop chime to the main run loop."""
    if _dispatcher is not None and _STOP_SOUND is not None:
        _dispatcher.performSelectorOnMainThread_withObject_waitUntilDone_(
            "playSound:", _STOP_SOUND, False
        )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  AUTO-UPDATE                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _check_and_update() -> None:
    """Fetch from origin; if behind, pull and restart the process."""
    try:
        subprocess.run(
            ["git", "fetch", "origin", "master"],
            cwd=_REPO_DIR, capture_output=True, timeout=10,
        )
        local  = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_DIR, text=True
        ).strip()
        remote = subprocess.check_output(
            ["git", "rev-parse", "origin/master"], cwd=_REPO_DIR, text=True
        ).strip()

        if local == remote:
            print(f"[voice_paste] v{VERSION} — up to date.")
            return

        print(f"[voice_paste] New version available — updating…")
        result = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "master"],
            cwd=_REPO_DIR, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"[voice_paste] Update failed:\n{result.stderr}")
            return

        # Reload version string after pull
        new_ver = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "?"
        print(f"[voice_paste] Updated to v{new_ver} — restarting…")
        time.sleep(0.5)   # let the log line flush
        os.execv(sys.executable, [sys.executable] + sys.argv)

    except Exception as exc:
        print(f"[voice_paste] Update check skipped: {exc}")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  AUDIO DEVICES                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _get_input_devices() -> list[tuple[int, str]]:
    """Return list of (index, name) for all input-capable audio devices."""
    devices = sd.query_devices()
    return [
        (i, d["name"])
        for i, d in enumerate(devices)
        if d["max_input_channels"] > 0
    ]


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  AUDIO CAPTURE                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _record_loop() -> None:
    global _frames
    _frames = []

    def _cb(indata, frames, t, status):
        if status:
            print(f"[voice_paste] Audio status: {status}", flush=True)
        if _recording:
            _frames.append(indata.copy())

    try:
        with sd.InputStream(
            device=_selected_device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=1024,
            callback=_cb,
        ):
            while _recording:
                time.sleep(0.01)
    except Exception as exc:
        print(f"[voice_paste] Audio capture error: {exc}", flush=True)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  TRANSCRIPTION + PASTE                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _transcribe_and_paste() -> None:
    global _transcribing
    if not _frames:
        print("[voice_paste] No audio captured.")
        return
    _transcribing = True

    audio = np.concatenate(_frames, axis=0).flatten()
    pcm   = (audio * 32_767).astype(np.int16)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name

    try:
        wavfile.write(tmp, SAMPLE_RATE, pcm)

        print("[voice_paste] Transcribing...", end="", flush=True)
        with open(tmp, "rb") as af:
            kw: dict = {"model": MODEL, "file": af}
            if LANGUAGE:
                kw["language"] = LANGUAGE
            result = client.audio.transcriptions.create(**kw)

        text = result.text.strip()
        print(f' done -> "{text}"')

        if text:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            time.sleep(0.08)
            kb = keyboard.Controller()
            with kb.pressed(keyboard.Key.cmd):
                kb.tap("v")
        else:
            print("[voice_paste] Whisper returned empty transcript.")

    except Exception as exc:
        print(f"\n[voice_paste] Error: {exc}", flush=True)
    finally:
        _transcribing = False
        os.unlink(tmp)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  RECORDING CONTROL                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _start_recording() -> None:
    global _recording, _rec_thread, _recording_start
    with _state_lock:
        if _recording:
            return
        _recording = True
        _recording_start = time.time()

    _rec_thread = threading.Thread(target=_record_loop, daemon=True)
    _rec_thread.start()
    print("[voice_paste] Recording started")


def _stop_recording() -> None:
    global _recording
    with _state_lock:
        if not _recording:
            return
        _recording = False
    print("[voice_paste] Recording stopped")

    if _rec_thread:
        _rec_thread.join(timeout=0.5)

    _play_stop_chime()
    threading.Thread(target=_transcribe_and_paste, daemon=True).start()


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  KEYBOARD LISTENER  (background thread)                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _on_press(key) -> None:
    global _ctrl_held, _space_held
    if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
        _ctrl_held = True
    if key == keyboard.Key.space:
        _space_held = True
    if _ctrl_held and _space_held:
        _start_recording()


def _on_release(key) -> None:
    global _ctrl_held, _space_held
    if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
        _ctrl_held = False
    if key == keyboard.Key.space:
        _space_held = False
    if not (_ctrl_held and _space_held):
        _stop_recording()


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  MENU BAR APP  (main thread — required by macOS)                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

_IDLE_TITLE = "🎙"

class VoicePasteApp(rumps.App):
    def __init__(self):
        # "VoicePaste" = app name (Activity Monitor + "Quit VoicePaste" label)
        # self.title overrides what's displayed in the menu bar itself
        super().__init__("VoicePaste")
        self.title = _IDLE_TITLE
        self._build_mic_menu()
        # Version info item (non-clickable)
        ver_item = rumps.MenuItem(f"VoicePaste v{VERSION}")
        ver_item.set_callback(None)
        self.menu.add(ver_item)
        self.menu.add(rumps.MenuItem("Restart", callback=self._on_restart))
        self.menu.add(rumps.separator)

    # ── Mic selector ─────────────────────────────────────────────────────
    def _build_mic_menu(self):
        devices = _get_input_devices()
        mic_menu = rumps.MenuItem("Microphone")

        # "Default" option at the top
        default_item = rumps.MenuItem("System Default", callback=self._on_mic_select)
        default_item._device_index = None
        default_item.state = 1   # checked by default
        mic_menu.add(default_item)
        mic_menu.add(rumps.separator)

        for idx, name in devices:
            item = rumps.MenuItem(name, callback=self._on_mic_select)
            item._device_index = idx
            item.state = 0
            mic_menu.add(item)

        self.menu.add(mic_menu)
        self.menu.add(rumps.separator)
        self._mic_menu = mic_menu

    def _on_restart(self, _):
        print("[voice_paste] Restarting…", flush=True)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _on_mic_select(self, sender):
        global _selected_device
        _selected_device = sender._device_index

        # Update checkmarks
        for key, item in self._mic_menu.items():
            if isinstance(item, rumps.MenuItem) and hasattr(item, "_device_index"):
                item.state = 1 if item._device_index == _selected_device else 0

        label = "System Default" if _selected_device is None else sender.title
        print(f"[voice_paste] Microphone set to: {label}")

    # ── Timer: updates icon on main thread every 100ms ───────────────────
    @rumps.timer(0.1)
    def sync_title(self, _):
        global _spinner_idx

        # Hide from Dock on first tick — main thread, fully initialised by now
        if not hasattr(self, "_dock_hidden"):
            try:
                import AppKit
                AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                    AppKit.NSApplicationActivationPolicyAccessory
                )
            except Exception:
                pass
            self._dock_hidden = True

        if _recording:
            elapsed = int(time.time() - _recording_start)
            self.title = f"🔴 {elapsed}s"
        elif _transcribing:
            self.title = _SPINNER[_spinner_idx % len(_SPINNER)]
            _spinner_idx += 1
        else:
            if self.title != _IDLE_TITLE:
                self.title = _IDLE_TITLE


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  ENTRY POINT                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════╝

if __name__ == "__main__":
    # Auto-update: runs synchronously so a restart happens before the UI appears.
    # Wrapped in a thread with a timeout guard so a slow network can't stall startup.
    update_thread = threading.Thread(target=_check_and_update, daemon=True)
    update_thread.start()
    update_thread.join(timeout=15)   # max 15 s wait; continue regardless

    listener = keyboard.Listener(on_press=_on_press, on_release=_on_release)
    listener.start()

    print("─────────────────────────────────────────────")
    print(f"  VoicePaste v{VERSION}  ready")
    print("  Hold  Ctrl+Space  to record, release to paste")
    print("  Menu bar: 🎙 idle  →  🔴 Ns recording  →  ⠸ transcribing")
    print("  Click menu bar icon to select mic or quit")
    print("─────────────────────────────────────────────\n")

    _app = VoicePasteApp()
    _app.run()
