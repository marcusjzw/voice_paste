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
import fcntl
import collections
import json

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
_stream: "sd.InputStream | None" = None
_state_lock       = threading.Lock()
_recording_start  = 0.0        # time.time() when recording began
_selected_device_name: str | None = None  # None = system default; else CoreAudio device name
_app              = None       # set after rumps app is created
_REPO_DIR         = pathlib.Path(__file__).parent

_error_until      = 0.0        # time.time() until which menu bar shows ⚠️
_ERROR_DISPLAY_S  = 4.0        # seconds to keep ⚠️ visible after a failure
_last_error_at    = 0.0        # time.time() of the most recent error chime
_ERROR_DEBOUNCE_S = 1.5        # min seconds between consecutive error signals
_LOCK_FILE        = pathlib.Path(__file__).parent / "voice_paste.lock"
_lock_fh          = None       # held-open file handle for the single-instance flock
_DEVICE_PREF_FILE = pathlib.Path(__file__).parent / "selected_device.txt"  # persisted mic choice
_USAGE_FILE       = pathlib.Path(__file__).parent / "usage.jsonl"  # local spend log (one JSON record per clip)

# gpt-4o-transcribe pricing (USD). The transcription API returns exact per-call
# token usage, so we price that directly; the per-minute rate is a fallback for
# when the API reports duration instead of tokens. Update if OpenAI changes it.
_PRICE_INPUT_PER_M  = 2.50     # USD per 1M input tokens (audio+text)
_PRICE_OUTPUT_PER_M = 10.00    # USD per 1M output tokens
_PRICE_PER_MINUTE   = 0.006    # USD per audio minute (fallback estimate)

_last_device_check  = 0.0      # time.time() of last device-list rescan
_DEVICE_REFRESH_S   = 2.0      # min seconds between mic-menu rebuilds
_known_device_names: tuple = ()  # cached snapshot of input device names

_level_peak         = 0.0      # loudest RMS seen since the last meter tick
_METER_GLYPHS       = "⣀⣤⣶⣿"   # 4 braille bar heights, low → high (finer than blocks)
_METER_WIDTH        = 10       # scrolling waveform columns shown in the menu bar
# Perceptual (dB) window: speech RMS spans a wide range, so a linear scale
# leaves the bar stuck near the bottom. Map RMS in dB across this window to the
# full bar height instead — quiet and loud both register.
_METER_DB_FLOOR     = -50.0    # RMS at/below this → shortest bar
_METER_DB_CEIL      = -20.0    # RMS at/above this → tallest bar
_level_history      = collections.deque(
    [_METER_GLYPHS[0]] * _METER_WIDTH, maxlen=_METER_WIDTH
)

_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_spinner_idx = 0
_starting = True     # True until startup (incl. update check) finishes — shows a loading spinner

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
    _ERROR_SOUND = _AppKit.NSSound.soundNamed_("Funk").copy()
    _ERROR_SOUND.setVolume_(0.6)
except Exception as _e:
    print(f"[voice_paste] Could not load chimes: {_e}", flush=True)
    _dispatcher = _STOP_SOUND = _ERROR_SOUND = None

def _play_stop_chime() -> None:
    """Dispatch Pop chime to the main run loop."""
    if _dispatcher is not None and _STOP_SOUND is not None:
        _dispatcher.performSelectorOnMainThread_withObject_waitUntilDone_(
            "playSound:", _STOP_SOUND, False
        )

def _play_error_chime() -> None:
    """Dispatch Funk chime to the main run loop."""
    if _dispatcher is not None and _ERROR_SOUND is not None:
        _dispatcher.performSelectorOnMainThread_withObject_waitUntilDone_(
            "playSound:", _ERROR_SOUND, False
        )

def _notify(title: str, message: str) -> None:
    """Post a transient macOS notification via osascript."""
    import json as _json
    try:
        script = (
            f"display notification {_json.dumps(message)} "
            f"with title {_json.dumps(title)}"
        )
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=2,
        )
    except Exception as exc:
        print(f"[voice_paste] Notification failed: {exc}", flush=True)

def _signal_capture_error(reason: str) -> None:
    """Surface a recording failure: log, chime, notify, flash ⚠️ in menu bar.

    Debounced so a tight retry loop (e.g. autorepeat key fires while the device
    is unavailable) cannot stutter the chime at the system key-repeat rate.
    """
    global _error_until, _last_error_at
    now = time.time()
    print(f"[voice_paste] Capture error: {reason}", flush=True)
    if now - _last_error_at < _ERROR_DEBOUNCE_S:
        # Still within the debounce window from the previous signal — keep ⚠️
        # visible but skip the chime + notification spam.
        _error_until = now + _ERROR_DISPLAY_S
        return
    _last_error_at = now
    _error_until = now + _ERROR_DISPLAY_S
    _play_error_chime()
    _notify("VoicePaste — microphone unavailable", reason)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  PERMISSIONS (macOS TCC)                                                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝
#
# VoicePaste needs two grants, both attached to the host .app bundle:
#   • Microphone    — to record audio
#   • Accessibility — for the global Ctrl+Space hotkey (pynput input monitoring)
#
# Running inside a real .app bundle (not a bare `python` LaunchAgent) is what
# lets macOS raise these prompts and list the app in System Settings. We request
# them *proactively* at startup so the user gets a deterministic dialog instead
# of having to spam the hotkey to coax the microphone prompt out.

def _open_privacy_pane(anchor: str) -> None:
    """Open a specific System Settings → Privacy & Security pane."""
    try:
        subprocess.run(
            ["open", f"x-apple.systempreferences:com.apple.preference.security?{anchor}"],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


def _ensure_microphone_access(timeout_s: float = 30.0) -> bool:
    """True if mic access is authorized. Raises the system prompt when the state
    is undetermined; opens the Microphone pane (and notifies) when denied."""
    try:
        import AVFoundation
        audio = AVFoundation.AVMediaTypeAudio
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(audio)
        # 0 notDetermined, 1 restricted, 2 denied, 3 authorized
        if status == 3:
            return True
        if status == 0:
            print("[voice_paste] Requesting microphone access…", flush=True)
            done = threading.Event()
            AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                audio, lambda granted: done.set()
            )
            done.wait(timeout=timeout_s)
            if AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(audio) == 3:
                print("[voice_paste] Microphone access granted.", flush=True)
                return True
        print("[voice_paste] Microphone access NOT granted.", flush=True)
        _notify("VoicePaste needs Microphone access",
                "Enable VoicePaste under Privacy & Security → Microphone.")
        _open_privacy_pane("Privacy_Microphone")
        return False
    except ImportError:
        return _warmup_microphone_fallback()
    except Exception as exc:
        print(f"[voice_paste] Mic permission check error: {exc}", flush=True)
        return True  # fail open — the recording path surfaces real device errors


def _warmup_microphone_fallback() -> bool:
    """Fallback when AVFoundation is unavailable: a short capture trips the prompt
    the first time, and pure-zero output betrays a missing grant."""
    try:
        dev, _ = _resolve_device_index(_selected_device_name)
        rec = sd.rec(int(0.3 * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                     channels=CHANNELS, dtype="float32", device=dev)
        sd.wait()
        if float(np.max(np.abs(rec))) == 0.0:
            _notify("VoicePaste needs Microphone access",
                    "Enable VoicePaste under Privacy & Security → Microphone.")
            _open_privacy_pane("Privacy_Microphone")
            return False
        return True
    except Exception:
        return True


def _ensure_accessibility_access() -> bool:
    """True if this process is a trusted Accessibility client (needed for the
    global hotkey). When untrusted, raises the system prompt and opens the pane."""
    try:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
        trusted = bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
        if not trusted:
            print("[voice_paste] Accessibility not granted — prompted.", flush=True)
            _notify("VoicePaste needs Accessibility access",
                    "Enable VoicePaste under Privacy & Security → Accessibility, "
                    "then quit and reopen VoicePaste.")
            _open_privacy_pane("Privacy_Accessibility")
        return trusted
    except Exception as exc:
        print(f"[voice_paste] Accessibility check error: {exc}", flush=True)
        return True


def _prompt_microphone_if_needed() -> None:
    """Raise the macOS microphone prompt — MUST run on the main thread.

    AVCaptureDevice.requestAccess only presents the TCC dialog while the main
    run loop is spinning. Issued from a background thread (or before rumps'
    run loop starts) it silently resolves to 'denied' and the app never even
    appears in System Settings → Privacy → Microphone. We call this right
    before _app.run() and return immediately; the completion handler is
    delivered — and the prompt shown — once the run loop starts.
    """
    try:
        import AVFoundation
        audio = AVFoundation.AVMediaTypeAudio
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(audio)
        # 0 notDetermined, 1 restricted, 2 denied, 3 authorized
        if status == 3:
            return
        if status == 0:
            print("[voice_paste] Requesting microphone access (prompt)…", flush=True)
            AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                audio,
                lambda granted: print(
                    f"[voice_paste] Microphone access {'granted' if granted else 'denied'}.",
                    flush=True),
            )
            return
        # denied / restricted — macOS won't re-prompt; guide the user to Settings.
        print("[voice_paste] Microphone access denied — opening Settings.", flush=True)
        _notify("VoicePaste needs Microphone access",
                "Enable VoicePaste under Privacy & Security → Microphone.")
        _open_privacy_pane("Privacy_Microphone")
    except Exception as exc:
        print(f"[voice_paste] Mic prompt error: {exc}", flush=True)


def _check_permissions() -> None:
    """Report permission status + ensure Accessibility at startup. The
    Microphone prompt is raised separately on the main thread (see
    _prompt_microphone_if_needed) because it needs the run loop; here we only
    read the current mic status so this is safe to run on a background thread."""
    acc = _ensure_accessibility_access()
    try:
        import AVFoundation
        mic = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeAudio) == 3
    except Exception:
        mic = True  # fail open — the recording path surfaces real device errors
    print(f"[voice_paste] Permissions — microphone: {'ok' if mic else 'pending'}, "
          f"accessibility: {'ok' if acc else 'MISSING'}", flush=True)


def _acquire_single_instance_lock() -> bool:
    """Try to take an exclusive flock so only one VoicePaste runs at a time.

    Returns True if acquired (or if locking is unsupported and we should fail
    open), False if another process already holds the lock.
    """
    global _lock_fh
    try:
        _lock_fh = open(_LOCK_FILE, "w")
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except Exception as exc:
        # Unexpected errors (e.g. fcntl unavailable) — don't block startup.
        print(f"[voice_paste] Lock setup error (continuing): {exc}", flush=True)
        return True
    try:
        _lock_fh.seek(0)
        _lock_fh.truncate()
        _lock_fh.write(f"{os.getpid()}\n")
        _lock_fh.flush()
    except Exception:
        pass
    return True


def _load_device_pref() -> None:
    """Restore the saved mic choice into _selected_device_name at startup.

    The selection lives only in memory otherwise, so without this it resets to
    the system default on every restart — and this daemon restarts often.
    """
    global _selected_device_name
    try:
        if _DEVICE_PREF_FILE.exists():
            name = _DEVICE_PREF_FILE.read_text().strip()
            _selected_device_name = name or None
            if _selected_device_name:
                print(f"[voice_paste] Restored mic preference: {_selected_device_name}", flush=True)
    except Exception as exc:
        print(f"[voice_paste] Could not read mic preference: {exc}", flush=True)


def _save_device_pref() -> None:
    """Persist the current mic choice so it survives restarts."""
    try:
        if _selected_device_name is None:
            _DEVICE_PREF_FILE.unlink(missing_ok=True)
        else:
            _DEVICE_PREF_FILE.write_text(_selected_device_name + "\n")
    except Exception as exc:
        print(f"[voice_paste] Could not save mic preference: {exc}", flush=True)


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


def _resolve_device_index(name: str | None) -> tuple[int | None, bool]:
    """Look up the *current* PortAudio index for a device name.

    Returns (device_index, found). For system default (name=None), returns
    (None, True). If the name is not present in the current device list,
    returns (None, False) so the caller can fall back to system default.
    """
    if name is None:
        return None, True
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("name") == name and d.get("max_input_channels", 0) > 0:
                return i, True
    except Exception as exc:
        print(f"[voice_paste] query_devices failed: {exc}", flush=True)
    return None, False


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  AUDIO CAPTURE                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _level_to_glyph(rms: float) -> str:
    """Map an RMS amplitude to a bar glyph on a perceptual (dB) scale."""
    if rms <= 1e-6:
        return _METER_GLYPHS[0]
    db = 20.0 * float(np.log10(rms))
    unit = (db - _METER_DB_FLOOR) / (_METER_DB_CEIL - _METER_DB_FLOOR)
    unit = min(1.0, max(0.0, unit))
    return _METER_GLYPHS[int(unit * (len(_METER_GLYPHS) - 1) + 0.5)]


def _audio_callback(indata, frames, t, status) -> None:
    """PortAudio callback — appends frames and tracks the live level meter."""
    global _level_peak
    if status:
        print(f"[voice_paste] Audio status: {status}", flush=True)
    if _recording:
        _frames.append(indata.copy())
        if indata.size:
            block_rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
            # Hold the loudest block since the last display tick; the meter
            # samples and resets this each tick. Peak (not average) keeps the
            # waveform lively and responsive to real speech transients.
            _level_peak = max(_level_peak, block_rms)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  TRANSCRIPTION + PASTE                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _send_cmd_v() -> None:
    """Synthesise ⌘V using a raw virtual keycode via Quartz.

    We deliberately avoid pynput's Controller.tap('v') here: mapping the
    *character* 'v' to a keycode makes pynput enumerate the Text Input Source
    (TSM / islGetInputSourceListWithAdditions), which asserts main-thread-only
    on recent macOS and crashes the process (SIGTRAP) when called from this
    background transcription thread. Posting the physical keycode
    (kVK_ANSI_V = 9) needs no input-source lookup and is safe from any thread.
    """
    import Quartz
    KEYCODE_V = 9  # kVK_ANSI_V — physical key, layout-independent
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    down = Quartz.CGEventCreateKeyboardEvent(src, KEYCODE_V, True)
    Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskCommand)
    up = Quartz.CGEventCreateKeyboardEvent(src, KEYCODE_V, False)
    Quartz.CGEventSetFlags(up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


# ── Usage / cost tracking ────────────────────────────────────────────────────

def _clip_cost(usage, duration_s: float) -> float:
    """USD cost of one transcription. Uses the API's reported token usage when
    available; falls back to audio duration × per-minute rate otherwise."""
    try:
        if usage is not None:
            utype = getattr(usage, "type", None)
            if utype == "tokens":
                inp = getattr(usage, "input_tokens", 0) or 0
                out = getattr(usage, "output_tokens", 0) or 0
                return (inp / 1e6 * _PRICE_INPUT_PER_M
                        + out / 1e6 * _PRICE_OUTPUT_PER_M)
            if utype == "duration":
                secs = getattr(usage, "seconds", 0) or 0
                return secs / 60.0 * _PRICE_PER_MINUTE
    except Exception:
        pass
    return (duration_s or 0.0) / 60.0 * _PRICE_PER_MINUTE


def _record_usage(usage, duration_s: float) -> None:
    """Append one clip's usage to the local spend log. Never raises."""
    try:
        rec = {
            "ts": time.time(),
            "dur_s": round(float(duration_s or 0.0), 3),
            "in_tok": int(getattr(usage, "input_tokens", 0) or 0) if usage else 0,
            "out_tok": int(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
            "cost": _clip_cost(usage, duration_s),
        }
        with open(_USAGE_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as exc:
        print(f"[voice_paste] usage log error: {exc}", flush=True)


def _fmt_usd(c: float) -> str:
    return f"${c:,.2f}" if c >= 1 else f"${c:.4f}"


def _usage_summary() -> str:
    """Human-readable day/week/month/all-time spend rollup for the menu alert."""
    if not _USAGE_FILE.exists():
        return ("No usage yet.\n\n"
                "Hold Ctrl+Space to transcribe something, then check back here.")
    now = time.time()
    windows = [("Last 24 hours", 86_400),
               ("Last 7 days", 7 * 86_400),
               ("Last 30 days", 30 * 86_400)]
    agg = {name: [0.0, 0, 0.0] for name, _ in windows}   # [cost, count, seconds]
    total = [0.0, 0, 0.0]
    first_ts = None
    try:
        with open(_USAGE_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts", 0.0)
                cost = float(rec.get("cost", 0.0) or 0.0)
                dur = float(rec.get("dur_s", 0.0) or 0.0)
                first_ts = ts if first_ts is None else min(first_ts, ts)
                total[0] += cost; total[1] += 1; total[2] += dur
                for name, span in windows:
                    if now - ts <= span:
                        b = agg[name]
                        b[0] += cost; b[1] += 1; b[2] += dur
    except Exception as exc:
        return f"Couldn't read usage log:\n{exc}"

    # Columns are laid out for a monospaced font (see _on_view_usage); the
    # alert renders this in SF Mono so everything lines up as a table.
    def _row(name, stats):
        c, n, d = stats
        return f"{name:<14}{_fmt_usd(c):>9}{n:>7}{d / 60:>8.1f}"

    lines = [f"{'':<14}{'Cost':>9}{'Clips':>7}{'Min':>8}"]
    lines += [_row(name, agg[name]) for name, _ in windows]
    lines.append("")
    lines.append(_row("All time", total))
    since = (time.strftime("%b %-d, %Y", time.localtime(first_ts))
             if first_ts else "today")
    lines.append(f"Since {since}")
    lines.append("")
    lines.append(f"Estimated from {MODEL} token usage.")
    return "\n".join(lines)


def _transcribe_and_paste() -> None:
    global _transcribing
    if not _frames:
        print("[voice_paste] No audio captured.")
        return
    _transcribing = True

    audio = np.concatenate(_frames, axis=0).flatten()
    pcm   = (audio * 32_767).astype(np.int16)
    duration_s = audio.size / SAMPLE_RATE if audio.size else 0.0

    # Report captured level. A peak of exactly 0.0 means the OS handed us pure
    # silence — almost always a missing Microphone grant, which would otherwise
    # surface only as garbled "hallucinated" transcripts.
    _peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if _peak == 0.0:
        print("[voice_paste] WARNING: captured pure silence (peak=0) — likely no "
              "Microphone permission. Check Privacy & Security → Microphone.",
              flush=True)
        _notify("VoicePaste captured silence",
                "No microphone signal — check Privacy & Security → Microphone.")

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

        _record_usage(getattr(result, "usage", None), duration_s)
        text = result.text.strip()
        print(f' done -> "{text}"')

        if text:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            time.sleep(0.08)
            _send_cmd_v()
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
    """Open the input stream synchronously and only enter recording state on success."""
    global _recording, _recording_start, _stream, _frames, _level_peak

    with _state_lock:
        if _recording:
            return

        device_index, found = _resolve_device_index(_selected_device_name)
        if _selected_device_name is not None and not found:
            _signal_capture_error(
                f"'{_selected_device_name}' is not connected — using system default"
            )
            device_index = None  # one-shot fallback; selection preserved for reconnect

        _frames = []
        _level_peak = 0.0
        _level_history.clear()
        _level_history.extend([_METER_GLYPHS[0]] * _METER_WIDTH)
        try:
            stream = sd.InputStream(
                device=device_index,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                blocksize=256,   # ~62 level updates/s @ 16 kHz — feeds the 30 Hz meter
                callback=_audio_callback,
            )
            stream.start()
        except Exception as exc:
            try:
                if "stream" in locals() and stream is not None:
                    stream.close()
            except Exception:
                pass
            _signal_capture_error(f"Could not open microphone: {exc}")
            return

        _stream = stream
        _recording = True
        _recording_start = time.time()

    print("[voice_paste] Recording started")


def _stop_recording() -> None:
    global _recording, _stream
    with _state_lock:
        if not _recording:
            return
        _recording = False
        stream = _stream
        _stream = None
        duration = time.time() - _recording_start

    if stream is not None:
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            print(f"[voice_paste] Error closing stream: {exc}", flush=True)

    print(f"[voice_paste] Recording stopped ({duration:.2f}s)")

    if _frames:
        _play_stop_chime()
        threading.Thread(target=_transcribe_and_paste, daemon=True).start()
    elif duration > 0.5:
        # Stream opened but produced zero frames over a held press — likely a silent
        # mid-recording failure. Surface it so the user doesn't lose the monologue.
        _signal_capture_error("No audio captured — the microphone may have disconnected mid-recording")
    # else: too short to matter (accidental tap), stay silent.


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
        self.menu.add(rumps.MenuItem("View Usage", callback=self._on_view_usage))
        self.menu.add(rumps.MenuItem("Restart", callback=self._on_restart))
        self.menu.add(rumps.separator)

    # ── Mic selector ─────────────────────────────────────────────────────
    def _build_mic_menu(self):
        self._mic_menu = rumps.MenuItem("Microphone")
        self.menu.add(self._mic_menu)
        self.menu.add(rumps.separator)
        self._populate_mic_menu()

    def _populate_mic_menu(self):
        """Fill the mic submenu with current input devices.

        Always shows: System Default + currently connected input devices.
        If the saved selection isn't currently connected, it's appended at
        the bottom marked '(disconnected)' so the user can see what they
        picked, and so reconnecting auto-resumes the choice.
        """
        global _known_device_names

        # MenuItem.clear() crashes if the underlying NSMenu hasn't been
        # created yet — that only happens once at least one child has been
        # added. Guard the first build.
        if getattr(self._mic_menu, "_menu", None) is not None:
            self._mic_menu.clear()

        devices = _get_input_devices()
        connected_names = [name for _, name in devices]
        _known_device_names = tuple(connected_names)

        default_item = rumps.MenuItem("System Default", callback=self._on_mic_select)
        default_item._device_name = None
        default_item.state = 1 if _selected_device_name is None else 0
        self._mic_menu.add(default_item)
        self._mic_menu.add(rumps.separator)

        for name in connected_names:
            item = rumps.MenuItem(name, callback=self._on_mic_select)
            item._device_name = name
            item.state = 1 if name == _selected_device_name else 0
            self._mic_menu.add(item)

        if (
            _selected_device_name is not None
            and _selected_device_name not in connected_names
        ):
            self._mic_menu.add(rumps.separator)
            ghost = rumps.MenuItem(
                f"{_selected_device_name} (disconnected)",
                callback=self._on_mic_select,
            )
            ghost._device_name = _selected_device_name
            ghost.state = 1
            self._mic_menu.add(ghost)

    def _on_view_usage(self, _):
        text = _usage_summary()
        try:
            import AppKit
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("VoicePaste usage")
            alert.addButtonWithTitle_("Close")

            # Monospaced accessory view so the columns line up as a table
            # (NSAlert's own text is proportional and can't align).
            font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(
                12.0, getattr(AppKit, "NSFontWeightRegular", 0.0))
            char_w = font.advancementForGlyph_(font.glyphWithName_("space")).width or 7.2
            maxlen = max((len(l) for l in text.splitlines()), default=24)
            n_lines = text.count("\n") + 1
            width = maxlen * char_w + 16
            height = n_lines * 16.0 + 6

            tf = AppKit.NSTextField.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, width, height))
            tf.setEditable_(False)
            tf.setSelectable_(True)
            tf.setBezeled_(False)
            tf.setDrawsBackground_(False)
            tf.setUsesSingleLineMode_(False)
            tf.setFont_(font)
            tf.setStringValue_(text)
            alert.setAccessoryView_(tf)
            alert.runModal()
        except Exception as exc:
            print(f"[voice_paste] usage view error: {exc}", flush=True)
            try:
                rumps.alert(title="VoicePaste usage", message=text, ok="Close")
            except Exception:
                pass

    def _on_restart(self, _):
        print("[voice_paste] Restarting…", flush=True)
        # Bounce the LaunchAgent so the fresh process starts in launchd's
        # session context (where the menu-bar icon registers correctly).
        # kickstart -k kills this instance and relaunches it.
        subprocess.Popen(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.voicepaste"],
            start_new_session=True,
            close_fds=True,
        )

    def _on_mic_select(self, sender):
        global _selected_device_name
        _selected_device_name = sender._device_name

        # Update checkmarks
        for _key, item in self._mic_menu.items():
            if isinstance(item, rumps.MenuItem) and hasattr(item, "_device_name"):
                item.state = 1 if item._device_name == _selected_device_name else 0

        label = "System Default" if _selected_device_name is None else sender.title
        _save_device_pref()
        print(f"[voice_paste] Microphone set to: {label}")

    # ── Timer: updates icon on main thread ~30×/s ────────────────────────
    @rumps.timer(0.033)
    def sync_title(self, _):
        global _spinner_idx, _last_device_check, _level_peak

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

        # Rescan input devices periodically; rebuild the mic menu on change.
        # Skipped during recording to avoid touching menu state mid-stream.
        now = time.time()
        if not _recording and now - _last_device_check >= _DEVICE_REFRESH_S:
            _last_device_check = now
            try:
                current = tuple(name for _, name in _get_input_devices())
                if current != _known_device_names:
                    self._populate_mic_menu()
            except Exception as exc:
                print(f"[voice_paste] Device refresh error: {exc}", flush=True)

        if _recording:
            elapsed = int(time.time() - _recording_start)
            # Sample-and-reset the peak level for this tick, then scroll it into
            # the waveform history so the bars flow left as you speak.
            peak = _level_peak
            _level_peak = 0.0
            _level_history.append(_level_to_glyph(peak))
            self.title = f"🔴 {elapsed}s {''.join(_level_history)}"
        elif _transcribing:
            self.title = _SPINNER[_spinner_idx % len(_SPINNER)]
            _spinner_idx += 1
        elif _starting:
            # Loading state while startup / update check runs.
            self.title = f"🎙 {_SPINNER[_spinner_idx % len(_SPINNER)]}"
            _spinner_idx += 1
        elif time.time() < _error_until:
            if self.title != "⚠️":
                self.title = "⚠️"
        else:
            if self.title != _IDLE_TITLE:
                self.title = _IDLE_TITLE


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  ENTRY POINT                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════╝

if __name__ == "__main__":
    # Refuse to run if another VoicePaste is already alive. Two daemons would
    # both grab the audio device on each Ctrl+Space — the loser ends up
    # rapid-firing the error chime at the keyboard autorepeat rate.
    if not _acquire_single_instance_lock():
        print(
            "[voice_paste] Another instance already holds the lock — exiting.",
            flush=True,
        )
        sys.exit(0)

    # Restore the saved mic choice before the listener/UI start.
    _load_device_pref()

    # Proactively request Microphone + Accessibility so the prompts appear
    # deterministically at launch (no need to spam the hotkey). Runs in a thread
    # so a slow user response can't delay the menu bar appearing.
    threading.Thread(target=_check_permissions, daemon=True).start()

    # Auto-update runs in the background so the menu-bar icon appears quickly;
    # the icon shows a loading spinner (see sync_title) until this finishes.
    def _startup_update():
        global _starting
        try:
            _check_and_update()
        finally:
            _starting = False
    threading.Thread(target=_startup_update, daemon=True).start()

    listener = keyboard.Listener(on_press=_on_press, on_release=_on_release)
    listener.start()

    print("─────────────────────────────────────────────")
    print(f"  VoicePaste v{VERSION}  ready")
    print("  Hold  Ctrl+Space  to record, release to paste")
    print("  Menu bar: 🎙 idle  →  🔴 Ns recording  →  ⠸ transcribing")
    print("  Click menu bar icon to select mic or quit")
    print("─────────────────────────────────────────────\n")

    _app = VoicePasteApp()
    # Raise the mic prompt on the MAIN THREAD; the dialog is presented once
    # _app.run() starts the run loop (see _prompt_microphone_if_needed).
    _prompt_microphone_if_needed()
    _app.run()
