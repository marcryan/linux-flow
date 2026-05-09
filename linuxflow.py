#!/usr/bin/env python3
"""
LinuxFlow - Offline voice dictation for Linux

Usage:
    python linuxflow.py                     # terminal mode (Enter to record, ESC exits)
    python linuxflow.py --daemon            # background mode (tray icon + hotkey)
    python linuxflow.py --model tiny        # faster, lower quality
    python linuxflow.py --model base        # balanced
    python linuxflow.py --language auto     # auto-detect language
    python linuxflow.py --devices           # list audio devices
    python linuxflow.py --transcript 5      # print last N saved transcripts (plain text)

Daemon mode:
    Hold Ctrl+Super+Z to record, release to stop + transcribe.
    Tray icons: idle (ready), rec (recording), pro (transcribing)—see README for default colors.
"""

import argparse
import contextlib
import configparser
import ctypes
from collections import deque
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import signal
import os
import re
import select
import selectors
import subprocess
import termios
import tty
import sys
import threading
import time

import numpy as np
import pyaudio
from asr import FasterWhisperBackend

# Optional imports for daemon mode
try:
    import evdev
    from evdev import InputDevice, ecodes
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib
    HAS_GLIB = True
except (ImportError, ValueError):
    HAS_GLIB = False

SUPPRESS_NATIVE_WARNINGS = os.environ.get("LINUXFLOW_SUPPRESS_ALSA_GTK_WARNINGS", "1").lower() not in {
    "0",
    "false",
    "no",
}

# Suppress noisy ALSA warnings on PipeWire systems (can be disabled via env var).
if SUPPRESS_NATIVE_WARNINGS:
    try:
        _asound = ctypes.cdll.LoadLibrary("libasound.so.2")
        _err_handler = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                         ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)(
            lambda *_: None
        )
        _asound.snd_lib_error_set_handler(_err_handler)
    except OSError:
        pass

# PortAudio may probe JACK before streams open; libjack prints "connect(...) failed" to stderr.
if SUPPRESS_NATIVE_WARNINGS:
    try:
        _jack = ctypes.CDLL("libjack.so.0")
        _jack_log_cb = ctypes.CFUNCTYPE(None, ctypes.c_char_p)(lambda *_: None)
        for _sym in ("jack_set_error_function", "jack_set_info_function"):
            _fn = getattr(_jack, _sym, None)
            if _fn:
                try:
                    _fn(_jack_log_cb)
                except Exception:
                    pass  # mismatched SONAME/API on some installs
    except OSError:
        pass

# Suppress GTK-CRITICAL warnings (cosmetic, from pystray on Wayland).
if SUPPRESS_NATIVE_WARNINGS:
    try:
        _gtk = ctypes.cdll.LoadLibrary("libgtk-3.so.0")
        _log_handler = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                         ctypes.c_char_p, ctypes.c_void_p)(
            lambda *_: None
        )
        _glib = ctypes.cdll.LoadLibrary("libglib-2.0.so.0")
        _glib.g_log_set_handler(b"Gtk", 1 << 4, _log_handler, None)  # G_LOG_LEVEL_CRITICAL
    except OSError:
        pass

# ---------- Config ----------
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK = 1024
FORMAT = pyaudio.paInt16
STATE_DIR = os.path.join(os.path.expanduser("~"), ".local", "state", "linuxflow")
PID_FILE = os.path.join(STATE_DIR, "linuxflow.pid")
TRANSCRIPT_LOG_PATH = os.path.join(STATE_DIR, "transcript_log.md")
# In-process heading cache avoids re-reading the file; duplicate "## date" sections can appear across restarts.
_transcript_log_last_heading_date = None
RECENT_TRANSCRIPTIONS_MAX = 5
RECENT_TRANSCRIPT_MENU_MAX_CHARS = 52
SOUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
# Returned by record_audio() when terminal user presses ESC during capture (discard, no transcript).
RECORD_CANCELLED = object()
MIN_DURATION = 0.2  # seconds - allow short one-word utterances
POST_RELEASE_BUFFER = 0.35  # seconds - capture trailing phonemes after key release
TRANSCRIBE_TAIL_PAD = 0.25  # seconds - append silence to preserve final token
ASR_TIMEOUT_S = 30.0
ASR_RETRIES = 1
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "linuxflow")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
LANGUAGE_OPTIONS = ["auto", "en", "es", "fr", "de", "hi"]
MODEL_OPTIONS = ["tiny", "base", "small", "medium", "large-v3-turbo"]
HOTKEY_OPTIONS = [
    "Ctrl+Super+Z",
    "Ctrl+Alt+Z",
    "Ctrl+Shift+Space",
    "Alt+Super+Z",
    "CapsLock",
]
ICON_THEME_OPTIONS = ["auto", "light", "dark"]
CONFIG_SCHEMA = {
    "model": {"default": "small", "type": str, "allowed": MODEL_OPTIONS},
    "language": {"default": "en", "type": str, "allowed": LANGUAGE_OPTIONS},
    "clipboard_enabled": {"default": True, "type": bool},
    "paste_enabled": {"default": True, "type": bool},
    "sound_notifications": {"default": False, "type": bool},
    "append_space": {"default": True, "type": bool},
    "release_tail_buffer_s": {"default": 0.55, "type": float, "min": 0.1, "max": 1.5},
    "hotkey": {"default": "Ctrl+Super+Z", "type": str, "validator": "hotkey"},
    "icon_theme": {"default": "auto", "type": str, "allowed": ICON_THEME_OPTIONS},
}

class _QuietStreamEventsFilter(logging.Filter):
    """Non-fatal clipboard/paste noise: keep in rotating file log, omit from tty StreamHandler."""

    _SUBSTRINGS = ("terminal_clipboard_copy_failed", "terminal_paste_fallback_failed")

    def filter(self, record):
        msg = record.getMessage()
        return not any(s in msg for s in self._SUBSTRINGS)


logger = logging.getLogger("linuxflow")
if not logger.handlers:
    os.makedirs(STATE_DIR, exist_ok=True)
    handler = logging.StreamHandler()
    handler.addFilter(_QuietStreamEventsFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s level=%(levelname)s event=%(message)s"))
    logger.addHandler(handler)
    file_handler = RotatingFileHandler(
        os.path.join(STATE_DIR, "linuxflow.log"),
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s level=%(levelname)s event=%(message)s"))
    logger.addHandler(file_handler)
logger.setLevel(logging.INFO)


def log_event(level, event, **fields):
    parts = [event] + [f"{k}={repr(v)}" for k, v in fields.items()]
    logger.log(level, " ".join(parts))


@contextlib.contextmanager
def _suppress_os_stderr():
    """Route OS-level stderr (PortAudio/JACK chatter, etc.) to /dev/null for this thread's process FD."""
    # Use fd 2 explicitly: some environments point sys.stderr elsewhere; C libs always use STDERR_FILENO.
    stderr_fd = 2
    saved = os.dup(stderr_fd)
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, stderr_fd)
        os.close(devnull)
        yield
    finally:
        os.dup2(saved, stderr_fd)
        os.close(saved)


class _TTYEscExit(Exception):
    """Internal: user pressed ESC — exit terminal mode."""

    pass


class _TTYRecordingCancelled(Exception):
    """Internal: user pressed ESC while waiting to stop recording (discard clip)."""


def _tty_was_lone_escape(stdin_fd) -> bool:
    """After reading ESC (0x1b), return True only for a lone ESC (quit). Consume CSI/SS3 arrow/function keys."""
    if not select.select([stdin_fd], [], [], 0.05)[0]:
        return True
    suffix = os.read(stdin_fd, 1).decode("latin-1", errors="ignore")
    if suffix not in ("[", "O"):
        log_event(logging.DEBUG, "tty_escape_non_csi_suffix", suffix=repr(suffix[:4]))
        return False
    rest = suffix
    for _ in range(24):
        if rest and rest[-1] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz~":
            log_event(logging.DEBUG, "tty_escape_sequence_ignored", seq=repr(rest[:48]))
            return False
        if not select.select([stdin_fd], [], [], 0.05)[0]:
            break
        rest += os.read(stdin_fd, 1).decode("latin-1", errors="ignore")
    log_event(logging.DEBUG, "tty_escape_sequence_ignored", seq=repr(rest[:48]))
    return False


def tty_prompt_enter_or_esc(prompt: str, *, esc_exits_terminal: bool = True):
    """
    Print prompt. Enter submits buffered line (may be empty).
    Lone ESC -> _TTYEscExit if esc_exits_terminal else _TTYRecordingCancelled.
    """
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        line = input(prompt)
        stripped = "" if line is None else line.rstrip("\r\n")
        if stripped.strip().lower() in {"esc", "exit", "quit", "q"}:
            raise _TTYEscExit()
        return stripped

    sys.stdout.write(prompt)
    sys.stdout.flush()
    old = termios.tcgetattr(fd)
    chars = []
    try:
        tty.setcbreak(fd)
        while True:
            ch_t = sys.stdin.read(1)
            if not ch_t:
                raise EOFError()
            code = ord(ch_t)
            if code in (10, 13):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(chars)
            if code == 27:
                sys.stdout.write("\n")
                sys.stdout.flush()
                if _tty_was_lone_escape(fd):
                    if esc_exits_terminal:
                        raise _TTYEscExit()
                    raise _TTYRecordingCancelled()
                continue
            if code in (8, 127):
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if 32 <= code < 127:
                c = chr(code)
                chars.append(c)
                sys.stdout.write(c)
                sys.stdout.flush()
            else:
                log_event(logging.DEBUG, "tty_ignore_control", code=code)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def ensure_transcript_log_file():
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(TRANSCRIPT_LOG_PATH):
        return
    with open(TRANSCRIPT_LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("# LinuxFlow transcript log\n\n")


def _warm_transcript_log_heading_cache_from_file():
    """If the daemon/CLI restarts mid-day, learn the newest ## heading so we don't stack duplicates."""
    global _transcript_log_last_heading_date
    if _transcript_log_last_heading_date is not None:
        return
    try:
        if not os.path.isfile(TRANSCRIPT_LOG_PATH):
            return
        size = os.path.getsize(TRANSCRIPT_LOG_PATH)
        if size == 0:
            return
        with open(TRANSCRIPT_LOG_PATH, "rb") as fh:
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode("utf-8", errors="replace")
        for raw in reversed(tail.splitlines()):
            line = raw.strip()
            if line.startswith("## ") and len(line) >= 13:
                _transcript_log_last_heading_date = line[3:].strip().split()[0]
                break
    except OSError:
        pass


def append_transcript_log(
    text: str,
    *,
    language,
    audio_duration_s,
    transcribe_duration_s,
    session,
    source,
):
    """Append one successful transcription to the dated log (plain append; no flock)."""
    global _transcript_log_last_heading_date
    cleaned = (text or "").strip()
    if not cleaned:
        return
    ensure_transcript_log_file()
    _warm_transcript_log_heading_cache_from_file()
    now = datetime.now()
    date_h = now.strftime("%Y-%m-%d")
    time_h = now.strftime("%H:%M:%S")
    meta_bits = [
        f"session={session}",
        f"source={source}",
        f"lang={language or '?'}",
        f"audio={audio_duration_s:.2f}s" if audio_duration_s is not None else None,
        f"asr={transcribe_duration_s:.2f}s" if transcribe_duration_s is not None else None,
    ]
    meta_line = " · ".join(b for b in meta_bits if b)
    chunks = []
    if _transcript_log_last_heading_date != date_h:
        if _transcript_log_last_heading_date is not None:
            chunks.append("\n")
        chunks.append(f"## {date_h}\n\n")
        _transcript_log_last_heading_date = date_h
    chunks.append(f"### {time_h}\n{meta_line}\n\n{cleaned}\n\n")
    payload = "".join(chunks)
    with open(TRANSCRIPT_LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()


def clear_transcript_log_file():
    """Remove all transcription entries from the log file (fresh header only) and reset day-heading cache."""
    global _transcript_log_last_heading_date
    os.makedirs(STATE_DIR, exist_ok=True)
    _transcript_log_last_heading_date = None
    with open(TRANSCRIPT_LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("# LinuxFlow transcript log\n\n")


def open_transcript_log_viewer():
    ensure_transcript_log_file()
    try:
        subprocess.Popen(
            ["xdg-open", TRANSCRIPT_LOG_PATH],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"Could not open transcript log: {e}")
        print(f"Path: {TRANSCRIPT_LOG_PATH}")


def format_transcript_tray_label(text, max_chars=None):
    """Single-line tray label with middle collapse; ellipsis when long."""
    if max_chars is None:
        max_chars = RECENT_TRANSCRIPT_MENU_MAX_CHARS
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return " "
    if len(collapsed) <= max_chars:
        return collapsed
    keep = max(1, max_chars - 1)
    return collapsed[:keep].rstrip() + "…"


def load_recent_transcript_texts_from_log(limit=RECENT_TRANSCRIPTIONS_MAX):
    """Read transcript_log.md tail; newest entries first (transcript bodies only)."""
    if limit <= 0:
        return []
    if not os.path.isfile(TRANSCRIPT_LOG_PATH):
        return []
    try:
        size = os.path.getsize(TRANSCRIPT_LOG_PATH)
        read_len = min(size, max(98304, 6144 + limit * 6144))
        with open(TRANSCRIPT_LOG_PATH, "rb") as fh:
            fh.seek(max(0, size - read_len))
            blob = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    newest_first = _transcript_bodies_newest_first_from_blob(blob, limit)
    if len(newest_first) < limit and read_len < size:
        try:
            with open(TRANSCRIPT_LOG_PATH, "rb") as fh:
                whole = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return newest_first
        newest_first = _transcript_bodies_newest_first_from_blob(whole, limit)
    return newest_first


def _transcript_bodies_newest_first_from_blob(blob, limit):
    """Parse transcript_log.md content; return up to ``limit`` bodies newest-first."""
    if limit <= 0:
        return []
    parts = re.split(r"(?m)^### \d{2}:\d{2}:\d{2}\r?\n", blob)
    newest_first = []
    for segment in reversed(parts):
        segment = segment.strip()
        if not segment:
            continue
        lines = segment.splitlines()
        if not lines:
            continue
        head = lines[0]
        if "session=" not in head and "source=" not in head:
            continue
        i = 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        body = "\n".join(lines[i:]).strip()
        if body:
            newest_first.append(body)
            if len(newest_first) >= limit:
                break
    return newest_first


def print_transcript_cli_recent(count):
    """Write the last ``count`` saved transcript texts to stdout (plain text only)."""
    if count < 1:
        raise ValueError("count must be >= 1")
    texts = load_recent_transcript_texts_from_log(limit=count)
    for i, block in enumerate(texts):
        if i:
            sys.stdout.write("\n")
        sys.stdout.write(block)
        if not block.endswith("\n"):
            sys.stdout.write("\n")


def _backup_broken_config_file():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_base = f"{CONFIG_FILE}.bad-{ts}"
    backup_path = backup_base
    suffix = 1
    while os.path.exists(backup_path):
        backup_path = f"{backup_base}-{suffix}"
        suffix += 1
    os.replace(CONFIG_FILE, backup_path)
    return backup_path


def _atomic_write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)
    dir_fd = os.open(os.path.dirname(path), os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _default_persistent_config():
    return {key: meta["default"] for key, meta in CONFIG_SCHEMA.items()}


def _validate_config_field(key, value):
    schema = CONFIG_SCHEMA[key]
    expected_type = schema["type"]

    if expected_type is bool:
        if not isinstance(value, bool):
            return False, f"bool:{key}", schema["default"]
        return True, None, value

    if expected_type is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, f"number:{key}", schema["default"]
        normalized = float(value)
        min_value = schema.get("min")
        max_value = schema.get("max")
        if min_value is not None and normalized < min_value:
            return False, f"number in range [{min_value}, {max_value}]", schema["default"]
        if max_value is not None and normalized > max_value:
            return False, f"number in range [{min_value}, {max_value}]", schema["default"]
        return True, None, normalized

    if not isinstance(value, expected_type):
        return False, expected_type.__name__, schema["default"]

    allowed = schema.get("allowed")
    if allowed is not None and value not in allowed:
        return False, f"one_of:{allowed}", schema["default"]

    if schema.get("validator") == "hotkey":
        normalized = normalize_hotkey_label(value)
        if not _is_parseable_hotkey(normalized):
            return False, "parseable hotkey", schema["default"]
        return True, None, normalized

    return True, None, value


def load_persistent_config():
    defaults = _default_persistent_config()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    if key not in CONFIG_SCHEMA:
                        log_event(logging.WARNING, "config_unknown_key_ignored", key=key)
                        continue
                    is_valid, expected, normalized = _validate_config_field(key, value)
                    if not is_valid:
                        log_event(
                            logging.WARNING,
                            "config_field_invalid",
                            key=key,
                            value=value,
                            expected=expected,
                            action="defaulted",
                        )
                        continue
                    defaults[key] = normalized
    except FileNotFoundError:
        pass
    except Exception as e:
        log_event(logging.WARNING, "config_load_failed", error=str(e))
        try:
            backup_path = _backup_broken_config_file()
            log_event(logging.WARNING, "config_backed_up_as_bad", backup_path=backup_path)
        except Exception as backup_err:
            log_event(
                logging.WARNING,
                "config_backup_failed",
                error=str(backup_err),
                source=CONFIG_FILE,
            )
    return defaults


def normalize_hotkey_label(label):
    """Convert 'ctrl+alt+x' to 'Ctrl+Alt+X' for consistent display."""
    if not isinstance(label, str):
        return label
    parts = [p.strip() for p in label.split("+") if p.strip()]
    out = []
    for p in parts:
        u = p.upper()
        if u in {"CTRL", "ALT", "SHIFT", "SUPER"}:
            out.append(u.capitalize())
        elif u.startswith("F") and u[1:].isdigit():
            out.append(u)
        elif u in {"SPACE", "TAB", "ENTER", "ESC"}:
            out.append(u.capitalize())
        else:
            out.append(u)
    return "+".join(out)


def prompt_for_hotkey(current_label):
    """Open a native dialog to ask for a hotkey string. Returns label or None."""
    prompt_text = (
        "Enter hotkey combo (modifiers + key)\n"
        "Examples: Ctrl+Super+Z, Ctrl+Alt+Space, Ctrl+Shift+F9, CapsLock\n"
        "Modifiers: Ctrl, Alt, Shift, Super\n"
        "Trigger: A-Z, 0-9, F1-F12, Space, Tab, Enter, Esc, CapsLock"
    )
    dialogs = [
        ["kdialog", "--title", "LinuxFlow Hotkey", "--inputbox", prompt_text, current_label],
        ["zenity", "--entry", "--title=LinuxFlow Hotkey",
         f"--text={prompt_text}", f"--entry-text={current_label}"],
    ]
    for cmd in dialogs:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, check=False
            )
            if result.returncode == 0:
                value = (result.stdout or "").strip()
                return value or None
            return None
        except FileNotFoundError:
            continue
        except Exception as e:
            log_event(logging.WARNING, "hotkey_prompt_failed", error=str(e))
            continue
    return None


def _is_parseable_hotkey(label):
    """Lightweight validation: trigger key, with optional modifiers."""
    if not isinstance(label, str):
        return False
    parts = [p.strip().upper() for p in label.split("+") if p.strip()]
    if len(parts) < 1:
        return False
    valid_modifiers = {"CTRL", "SUPER", "ALT", "SHIFT"}
    valid_triggers = (
        set(chr(c) for c in range(ord("A"), ord("Z") + 1))
        | set(str(d) for d in range(0, 10))
        | set(f"F{n}" for n in range(1, 13))
        | {"SPACE", "TAB", "ENTER", "ESC", "CAPSLOCK", "CAPS"}
    )
    if parts[-1] not in valid_triggers:
        return False
    for mod in parts[:-1]:
        if mod not in valid_modifiers:
            return False
    return True


def save_persistent_config(cfg):
    _atomic_write_json(CONFIG_FILE, cfg)


def _prompt_choice(options_count, allow_back=False):
    while True:
        raw = input("> ").strip()
        if allow_back and raw.lower() in {"b", "back"}:
            return None
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= options_count:
                return idx - 1
        print("Invalid selection. Enter a valid number.")


def _prompt_yes_no(message):
    while True:
        raw = input(f"{message} [y/N]: ").strip().lower()
        if raw in {"y", "yes"}:
            return True
        if raw in {"", "n", "no"}:
            return False
        print("Please enter y or n.")


def restart_linuxflow_service():
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "linuxflow.service"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        return False


def user_service_autostart_enabled():
    """True if systemd user unit is configured to start at graphical login (~/.config/systemd/user)."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", "linuxflow.service"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        state = (result.stdout or "").strip()
        # enabled, alias, enabled-runtime, etc. treated as yes; static/masked already non-zero RC
        return bool(state)
    except Exception:
        return False


def set_user_service_autostart(enable: bool) -> bool:
    """Enable or disable autostart via systemd user unit (survives reboot / next login session)."""
    cmd = (
        ["systemctl", "--user", "enable", "linuxflow.service"]
        if enable
        else ["systemctl", "--user", "disable", "linuxflow.service"]
    )
    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def configure_settings_menu():
    """Interactive terminal settings menu for tray-equivalent options."""
    cfg = load_persistent_config()

    def _print_header():
        print("\n=== LinuxFlow Configuration ===")
        print(f"1) Model               [{cfg['model']}]")
        print(f"2) Language            [{cfg['language']}]")
        print(f"3) Hotkey              [{cfg['hotkey']}]")
        print(f"4) Copy to Clipboard   [{'ON' if cfg['clipboard_enabled'] else 'OFF'}]")
        print(f"5) Auto Paste          [{'ON' if cfg['paste_enabled'] else 'OFF'}]")
        print(f"6) Sound Notifications [{'ON' if cfg.get('sound_notifications', False) else 'OFF'}]")
        au = user_service_autostart_enabled()
        print(f"7) Start at Login     [{'ON' if au else 'OFF'}]")
        print("8) Exit Config")
        print("Choose an item number:")

    while True:
        _print_header()
        choice = _prompt_choice(8)
        requires_restart = False

        if choice == 0:
            print("\nModel options:")
            for i, model in enumerate(MODEL_OPTIONS, 1):
                print(f"{i}) {model}")
            print("Select model number:")
            selected = _prompt_choice(len(MODEL_OPTIONS))
            new_model = MODEL_OPTIONS[selected]
            if cfg["model"] != new_model:
                cfg["model"] = new_model
                save_persistent_config(cfg)
                print(f"Saved: model = {cfg['model']}")
                requires_restart = True
            else:
                print("No changes made.")

        elif choice == 1:
            print("\nLanguage options:")
            for i, lang in enumerate(LANGUAGE_OPTIONS, 1):
                print(f"{i}) {lang}")
            print("Select language number:")
            selected = _prompt_choice(len(LANGUAGE_OPTIONS))
            new_language = LANGUAGE_OPTIONS[selected]
            if cfg["language"] != new_language:
                cfg["language"] = new_language
                save_persistent_config(cfg)
                print(f"Saved: language = {cfg['language']}")
                requires_restart = True
            else:
                print("No changes made.")

        elif choice == 2:
            print("\nHotkey options:")
            for i, hotkey in enumerate(HOTKEY_OPTIONS, 1):
                print(f"{i}) {hotkey}")
            print(f"{len(HOTKEY_OPTIONS) + 1}) Custom...")
            print("Select hotkey number:")
            selected = _prompt_choice(len(HOTKEY_OPTIONS) + 1)
            if selected < len(HOTKEY_OPTIONS):
                new_hotkey = HOTKEY_OPTIONS[selected]
                if cfg["hotkey"] != new_hotkey:
                    cfg["hotkey"] = new_hotkey
                    save_persistent_config(cfg)
                    print(f"Saved: hotkey = {cfg['hotkey']}")
                    requires_restart = True
                else:
                    print("No changes made.")
            else:
                print("Enter custom hotkey (example: Ctrl+Alt+X or CapsLock):")
                raw = input("> ").strip()
                normalized = normalize_hotkey_label(raw)
                if _is_parseable_hotkey(normalized):
                    if cfg["hotkey"] != normalized:
                        cfg["hotkey"] = normalized
                        save_persistent_config(cfg)
                        print(f"Saved: hotkey = {cfg['hotkey']}")
                        requires_restart = True
                    else:
                        print("No changes made.")
                else:
                    print("Invalid hotkey format. No changes made.")

        elif choice == 3:
            print("\nCopy to Clipboard:")
            print("1) ON")
            print("2) OFF")
            selected = _prompt_choice(2)
            new_clipboard_enabled = (selected == 0)
            if cfg["clipboard_enabled"] != new_clipboard_enabled:
                cfg["clipboard_enabled"] = new_clipboard_enabled
                if not cfg["clipboard_enabled"]:
                    cfg["paste_enabled"] = False
                save_persistent_config(cfg)
                print(f"Saved: clipboard_enabled = {cfg['clipboard_enabled']}")
                requires_restart = True
            else:
                print("No changes made.")

        elif choice == 4:
            print("\nAuto Paste:")
            print("1) ON")
            print("2) OFF")
            selected = _prompt_choice(2)
            new_paste_enabled = (selected == 0)
            if cfg["paste_enabled"] != new_paste_enabled:
                cfg["paste_enabled"] = new_paste_enabled
                if cfg["paste_enabled"]:
                    cfg["clipboard_enabled"] = True
                save_persistent_config(cfg)
                print(f"Saved: paste_enabled = {cfg['paste_enabled']}")
                requires_restart = True
            else:
                print("No changes made.")

        elif choice == 5:
            print("\nSound Notifications:")
            print("1) ON")
            print("2) OFF")
            selected = _prompt_choice(2)
            new_sound_notifications = (selected == 0)
            if bool(cfg.get("sound_notifications", False)) != new_sound_notifications:
                cfg["sound_notifications"] = new_sound_notifications
                save_persistent_config(cfg)
                print(f"Saved: sound_notifications = {cfg['sound_notifications']}")
                requires_restart = True
            else:
                print("No changes made.")

        elif choice == 6:
            print("\nStart LinuxFlow automatically at graphical login (systemd user unit):")
            print("1) ON")
            print("2) OFF")
            selected = _prompt_choice(2)
            enabling = selected == 0
            if user_service_autostart_enabled() != enabling:
                if set_user_service_autostart(enabling):
                    tag = "enabled" if enabling else "disabled"
                    print(f"Saved: systemd user autostart {tag}.")
                    if not enabling:
                        print("(Still running until you quit or restart the unit.)")
                else:
                    print("Could not change autostart — run:")
                    pref = "enable" if enabling else "disable"
                    print(f"  systemctl --user {pref} linuxflow.service")
            else:
                print("No changes made.")

        else:
            print("Exiting LinuxFlow configuration.")
            return

        if requires_restart and _prompt_yes_no("Restart LinuxFlow service now to apply this change?"):
            if restart_linuxflow_service():
                print("LinuxFlow service restarted.")
            else:
                print("Could not restart service automatically. Run: systemctl --user restart linuxflow.service")


# ---------- Core functions ----------

def list_devices():
    """Print available audio input devices."""
    with _suppress_os_stderr():
        p = pyaudio.PyAudio()
        print("Audio input devices:")
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                marker = " <-- default" if i == p.get_default_input_device_info()["index"] else ""
                print(f"  [{i}] {info['name']} ({info['maxInputChannels']}ch){marker}")
        p.terminate()


def record_audio(device_index=None):
    """
    Record audio from microphone until Enter (stop) or ESC (cancel recording).
    Returns float32 numpy array, None on error / empty capture, or RECORD_CANCELLED if user pressed ESC while stopping.
    """
    kwargs = dict(
        format=FORMAT,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )
    if device_index is not None:
        kwargs["input_device_index"] = device_index

    p = None
    cancelled = False
    frames = []
    try:
        # PyAudio() runs Pa_Initialize() and host-API probes (JACK "connect failed" spam) — must be inside suppress.
        with _suppress_os_stderr():
            p = pyaudio.PyAudio()
            stream = p.open(**kwargs)
            is_recording = True

            def capture():
                while is_recording:
                    try:
                        data = stream.read(CHUNK, exception_on_overflow=False)
                        frames.append(data)
                    except Exception:
                        break

            thread = threading.Thread(target=capture, daemon=True)
            thread.start()

            try:
                tty_prompt_enter_or_esc(
                    "  Press Enter to stop recording (ESC to discard this clip)...\n",
                    esc_exits_terminal=False,
                )
            except _TTYRecordingCancelled:
                cancelled = True
                log_event(logging.INFO, "terminal_recording_cancelled_esc")
            finally:
                is_recording = False
                thread.join(timeout=2)
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
    except OSError as e:
        log_event(logging.ERROR, "terminal_mic_open_failed", error=str(e))
        print(f"  Error opening mic: {e}")
        return None
    finally:
        if p is not None:
            with _suppress_os_stderr():
                try:
                    p.terminate()
                except Exception:
                    pass

    if cancelled:
        return RECORD_CANCELLED

    if not frames:
        return None

    audio = np.frombuffer(b"".join(frames), dtype=np.int16)
    audio = audio.astype(np.float32) / 32768.0
    return audio


def format_for_insert(text, append_space=True):
    """Prepare transcript text for clipboard/paste insertion."""
    cleaned = text.rstrip()
    if append_space:
        return cleaned + " "
    return cleaned


def copy_to_clipboard(text):
    """Copy text to clipboard (Wayland: wl-copy, fallback: xclip)."""
    # Short wl-copy timeout: a wedged compositor/session blocks for seconds and stalls the UI thread.
    attempts = (
        (["wl-copy", "--"], 0.85),
        (["xclip", "-selection", "clipboard"], 2.0),
    )
    for cmd, timeout_s in attempts:
        try:
            subprocess.run(
                cmd,
                input=text,
                text=True,
                timeout=timeout_s,
                check=True,
                capture_output=True,
            )
            return True
        except FileNotFoundError:
            log_event(logging.DEBUG, "clipboard_backend_missing", cmd=cmd[0])
        except subprocess.CalledProcessError as e:
            err = (getattr(e, "stderr", None) or b"") or (getattr(e, "stdout", None) or b"")
            if isinstance(err, bytes):
                err = err.decode("utf-8", errors="replace")
            log_event(logging.DEBUG, "clipboard_copy_failed", cmd=cmd[0], stderr=str(err)[:300])
        except subprocess.TimeoutExpired as e:
            err_parts = []
            for attr in ("stderr", "stdout"):
                chunk = getattr(e, attr, None)
                if chunk:
                    err_parts.append(
                        chunk.decode("utf-8", errors="replace")
                        if isinstance(chunk, bytes)
                        else str(chunk)
                    )
            log_event(
                logging.DEBUG,
                "clipboard_copy_timeout",
                cmd=cmd[0],
                snippet=(("; ".join(err_parts))[:300] if err_parts else ""),
            )
    return False


def type_text(text):
    """Type text into focused window using ydotool."""
    try:
        subprocess.run(
            ["ydotool", "type", "--key-delay", "3", "--", text],
            timeout=30,
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except FileNotFoundError:
        log_event(logging.DEBUG, "ydotool_type_missing")
        return False
    except subprocess.CalledProcessError as e:
        err = ((e.stderr or "") + (e.stdout or "")).strip()
        log_event(logging.DEBUG, "ydotool_type_failed", stderr=err[:400])
        return False


def paste_from_clipboard(text=None):
    """Paste clipboard content into focused window, fallback to typing text."""
    # Prefer ydotool (Wayland-friendly), fallback to xdotool (X11).
    try:
        subprocess.run(
            ["ydotool", "key", "--key-delay", "3", "29:1", "47:1", "47:0", "29:0"],  # Ctrl+V
            timeout=5,
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except FileNotFoundError:
        log_event(logging.DEBUG, "ydotool_key_missing")
    except subprocess.CalledProcessError as e:
        err = ((e.stderr or "") + (e.stdout or "")).strip()
        log_event(logging.DEBUG, "ydotool_key_failed", stderr=err[:400])
    except subprocess.TimeoutExpired:
        log_event(logging.DEBUG, "ydotool_key_timeout")

    try:
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
            timeout=5,
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except FileNotFoundError:
        log_event(logging.DEBUG, "xdotool_missing")
    except subprocess.CalledProcessError as e:
        err = ((e.stderr or "") + (e.stdout or "")).strip()
        log_event(logging.DEBUG, "xdotool_ctrlv_failed", stderr=err[:400])
    except subprocess.TimeoutExpired:
        log_event(logging.DEBUG, "xdotool_ctrlv_timeout")

    # Some Wayland sessions block synthetic paste shortcuts.
    # Final fallback: type the transcript directly.
    if text:
        return type_text(text)
    return False


def play_notification_sound(sound_path):
    """Play one notification sound asynchronously."""
    if not sound_path or not os.path.exists(sound_path):
        return False
    players = [
        ["pw-play", sound_path],
        ["paplay", sound_path],
        ["aplay", "-q", sound_path],
    ]
    for cmd in players:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return False


def get_recorder_command(output_path):
    """Return the best available recording command for this Linux host."""
    # Prefer parecord when available (PulseAudio/PipeWire compat layer),
    # then fall back to pw-record on pure PipeWire setups.
    recorders = [
        ["parecord", "--channels=1", "--rate=16000", "--format=s16le", "--file-format=wav", output_path],
        ["pw-record", "--channels", "1", "--rate", "16000", output_path],
    ]
    for cmd in recorders:
        try:
            subprocess.run([cmd[0], "--help"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            return cmd
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
    return None


def stop_recorder_process(proc):
    """Stop recorder process with graceful signal first, then force kill."""
    if not proc:
        return
    try:
        # parecord/pw-record flush more reliably on SIGINT than SIGTERM.
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=2.0)
        return
    except Exception:
        pass

    try:
        proc.terminate()
        proc.wait(timeout=2.0)
        return
    except Exception:
        pass

    try:
        proc.kill()
        proc.wait(timeout=1.0)
    except Exception:
        pass


def notify(title, body):
    """Send desktop notification."""
    try:
        subprocess.run(
            ["notify-send", "-t", "3000", "-a", "LinuxFlow", title, body],
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def write_pid_file():
    """Persist daemon pid for safer stop operations."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def remove_pid_file():
    """Best-effort cleanup of daemon pid file."""
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass


# ---------- Daemon mode ----------

def find_keyboards():
    """Find all keyboard input devices."""
    keyboards = []
    for path in evdev.list_devices():
        dev = None
        try:
            dev = InputDevice(path)
            caps = dev.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_A in caps and ecodes.KEY_SPACE in caps:
                keyboards.append(dev)
            else:
                dev.close()
        except Exception:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
    return keyboards


def make_icon(color):
    """Create a colored circle icon for the system tray."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = {
        "green": (76, 175, 80),
        "red": (244, 67, 54),
        "orange": (255, 152, 0),
    }
    draw.ellipse([4, 4, 60, 60], fill=colors.get(color, (128, 128, 128)))
    return img


def _read_cmd_output(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1.5, check=False)
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "").strip().strip("'").strip('"')


def prefers_dark_panel():
    """Best-effort desktop theme probe. Returns True if dark panel/theme is likely."""
    # GNOME/libadwaita standard
    color_scheme = _read_cmd_output(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]).lower()
    if "prefer-dark" in color_scheme:
        return True
    if "prefer-light" in color_scheme:
        return False

    # Legacy GTK theme naming convention
    gtk_theme = _read_cmd_output(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"]).lower()
    if "-dark" in gtk_theme or "dark" in gtk_theme:
        return True
    if gtk_theme:
        return False

    # KDE fallback
    kde_globals = os.path.join(os.path.expanduser("~"), ".config", "kdeglobals")
    if os.path.exists(kde_globals):
        try:
            cfg = configparser.ConfigParser()
            cfg.read(kde_globals, encoding="utf-8")
            scheme = cfg.get("General", "ColorScheme", fallback="").lower()
            if "dark" in scheme:
                return True
            if scheme:
                return False
        except Exception:
            pass

    return False


def load_tray_icons(icon_theme="auto"):
    """
    Load tray icons from icons/ based on desktop theme.
    Uses light-* assets on dark panels and dark-* assets on light panels.
    """
    icon_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    selected_theme = icon_theme if icon_theme in {"light", "dark"} else "auto"
    if selected_theme == "auto":
        variant = "light" if prefers_dark_panel() else "dark"
    else:
        variant = selected_theme
    state_to_name = {"green": "idle", "red": "rec", "orange": "pro"}
    sizes = [64, 128, 48, 512, 1024]
    loaded = {}

    for color, state in state_to_name.items():
        chosen = None
        for size in sizes:
            candidate = os.path.join(icon_root, f"icon-{variant}-{state}-{size}.png")
            if os.path.exists(candidate):
                try:
                    chosen = Image.open(candidate).convert("RGBA")
                    break
                except Exception:
                    continue
        loaded[color] = chosen or make_icon(color)

    return loaded, variant, selected_theme


def daemon_mode(args):
    """Run as background daemon with system tray and global hotkey."""
    if not HAS_EVDEV:
        print("Error: evdev not installed. Run: pip install evdev")
        sys.exit(1)
    if not HAS_TRAY:
        print("Error: pystray/Pillow not installed. Run: pip install pystray Pillow")
        sys.exit(1)
    if os.environ.get("WAYLAND_DISPLAY") and not HAS_GLIB:
        log_event(
            logging.WARNING,
            "tray_glib_missing",
            hint="Install python-gobject/python3-gi and reinstall to improve tray menu behavior on Wayland",
        )

    persisted = load_persistent_config()
    provided_flags = getattr(args, "_provided_flags", set())
    if "--model" in provided_flags and args.model in MODEL_OPTIONS:
        persisted["model"] = args.model
    if "--language" in provided_flags and args.language in LANGUAGE_OPTIONS:
        persisted["language"] = args.language
    if "--hotkey" in provided_flags and args.hotkey in HOTKEY_OPTIONS:
        persisted["hotkey"] = args.hotkey
    if "--no-clipboard" in provided_flags:
        persisted["clipboard_enabled"] = False
    if "--no-paste" in provided_flags:
        persisted["paste_enabled"] = False
    save_persistent_config(persisted)

    settings_lock = threading.Lock()
    settings = persisted
    print(f"Loading model '{settings['model']}'...")
    asr_backend = FasterWhisperBackend(
        model_name=settings["model"],
        device="cpu",
        compute_type="int8",
        sample_rate=SAMPLE_RATE,
        transcribe_tail_pad=TRANSCRIBE_TAIL_PAD,
        request_timeout_s=args.asr_timeout,
        max_retries=args.asr_retries,
    )
    print("Model loaded.")

    keyboards = find_keyboards()
    if not keyboards:
        print("Error: No keyboard found. Are you in the 'input' group?")
        print("  Fix: sudo usermod -aG input $USER")
        print("  Then log out and log back in.")
        sys.exit(1)
    for kb in keyboards:
        print(f"Keyboard: {kb.name} ({kb.path})")

    MODIFIER_KEYS = {
        "CTRL": {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL},
        "SUPER": {ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA},
        "ALT": {ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT},
        "SHIFT": {ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT},
    }
    trigger_key_map = {chr(code): getattr(ecodes, f"KEY_{chr(code)}") for code in range(ord("A"), ord("Z") + 1)}
    for digit in range(0, 10):
        trigger_key_map[str(digit)] = getattr(ecodes, f"KEY_{digit}")
    for fn in range(1, 13):
        trigger_key_map[f"F{fn}"] = getattr(ecodes, f"KEY_F{fn}")
    trigger_key_map["SPACE"] = ecodes.KEY_SPACE
    trigger_key_map["TAB"] = ecodes.KEY_TAB
    trigger_key_map["ENTER"] = ecodes.KEY_ENTER
    trigger_key_map["ESC"] = ecodes.KEY_ESC
    trigger_key_map["CAPSLOCK"] = ecodes.KEY_CAPSLOCK
    trigger_key_map["CAPS"] = ecodes.KEY_CAPSLOCK

    hotkey_state = {"label": "Ctrl+Super+Z", "required_modifiers": ["CTRL", "SUPER"], "trigger_key": ecodes.KEY_Z}
    hotkey_lock = threading.Lock()

    def apply_hotkey(label):
        parts = [p.strip().upper() for p in label.split("+") if p.strip()]
        if len(parts) < 1:
            return False
        trigger_name = parts[-1]
        modifiers = parts[:-1]
        for mod in modifiers:
            if mod not in MODIFIER_KEYS:
                return False
        trigger_code = trigger_key_map.get(trigger_name)
        if trigger_code is None:
            return False
        with hotkey_lock:
            hotkey_state["label"] = label
            hotkey_state["required_modifiers"] = modifiers
            hotkey_state["trigger_key"] = trigger_code
        return True

    if not apply_hotkey(settings["hotkey"]):
        apply_hotkey("Ctrl+Super+Z")

    # Shared state
    recording = False
    recording_lock = threading.Lock()
    rec_process = None
    rec_tmpfile = None
    tray = None
    session_count = 0
    shutdown_event = threading.Event()
    recent_transcripts = deque(maxlen=RECENT_TRANSCRIPTIONS_MAX)
    recent_transcripts_lock = threading.Lock()
    tray_menu_refresh = None

    def remember_recent_transcription(full_text):
        snippet = (full_text or "").strip()
        if not snippet:
            return
        snippet = snippet.replace("\x00", "")
        with recent_transcripts_lock:
            recent_transcripts.appendleft(snippet)
        updater = tray_menu_refresh
        if updater:
            updater()

    # Pre-create icons to avoid GTK calls from threads
    icons, theme_variant, icon_theme_mode = load_tray_icons(settings.get("icon_theme", "auto"))
    tray_state = {"color": "green", "title": "Ready"}
    log_event(
        logging.INFO,
        "tray_icons_loaded",
        theme_variant=theme_variant,
        icon_theme_mode=icon_theme_mode,
    )

    def current_hotkey_label():
        with hotkey_lock:
            return hotkey_state["label"]

    def set_tray(color, title):
        tray_state["color"] = color
        tray_state["title"] = title
        def _update():
            if tray:
                tray.icon = icons[color]
                tray.title = f"LinuxFlow - {title}"
            return False
        if HAS_GLIB:
            GLib.idle_add(_update)
        elif tray:
            tray.icon = icons[color]
            tray.title = f"LinuxFlow - {title}"

    def refresh_ready_tray():
        set_tray("green", f"Ready ({current_hotkey_label()})")

    def start_recording():
        nonlocal recording, rec_process, rec_tmpfile
        with recording_lock:
            if recording:
                return
            recording = True

            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                rec_tmpfile = tmp.name

            recorder_cmd = get_recorder_command(rec_tmpfile)
            if not recorder_cmd:
                print("  Error: no recorder found. Install 'pulseaudio' (parecord) or 'pipewire' (pw-record).")
                log_event(logging.ERROR, "recorder_missing")
                recording = False
                try:
                    os.unlink(rec_tmpfile)
                except OSError:
                    pass
                rec_tmpfile = None
                return
            try:
                rec_process = subprocess.Popen(recorder_cmd, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"  Error starting recorder: {e}")
                log_event(logging.ERROR, "recorder_start_failed", error=str(e))
                recording = False
                try:
                    os.unlink(rec_tmpfile)
                except OSError:
                    pass
                rec_tmpfile = None
                refresh_ready_tray()
                return

        set_tray("red", "Recording...")
        with settings_lock:
            local_sound_notifications = bool(settings.get("sound_notifications"))
        if local_sound_notifications:
            play_notification_sound(os.path.join(SOUND_DIR, "start.wav"))

    def stop_recording():
        nonlocal recording, rec_process, rec_tmpfile, session_count
        with recording_lock:
            if not recording:
                return
            recording = False
        with settings_lock:
            local_sound_notifications = bool(settings.get("sound_notifications"))
        if local_sound_notifications:
            play_notification_sound(os.path.join(SOUND_DIR, "stop.wav"))

        if rec_process:
            # Hold recording a bit after key release to avoid clipping final phonemes.
            with settings_lock:
                tail_buffer_s = float(settings.get("release_tail_buffer_s", POST_RELEASE_BUFFER))
            time.sleep(max(0.1, min(1.5, tail_buffer_s)))
            stop_recorder_process(rec_process)
            rec_process = None

        set_tray("orange", "Transcribing...")

        # Read the recorded wav file
        if not rec_tmpfile:
            refresh_ready_tray()
            return
        try:
            import wave
            with wave.open(rec_tmpfile, "rb") as wf:
                raw = wf.readframes(wf.getnframes())
                if len(raw) < SAMPLE_RATE * MIN_DURATION * 2:  # 2 bytes per sample
                    refresh_ready_tray()
                    return
                audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception as e:
            print(f"  Error reading audio: {e}")
            log_event(logging.ERROR, "recording_read_failed", error=str(e))
            refresh_ready_tray()
            return
        finally:
            try:
                os.unlink(rec_tmpfile)
            except OSError:
                pass
            rec_tmpfile = None

        duration = len(audio) / SAMPLE_RATE
        with settings_lock:
            local_clipboard_enabled = settings["clipboard_enabled"]
            local_paste_enabled = settings["paste_enabled"]
            local_append_space = bool(settings.get("append_space", True))
            local_language = settings["language"]
        lang_local = None if local_language == "auto" else local_language

        # Always return tray to Ready after transcribing (even if log/clipboard/paste fails).
        try:
            # Use the currently selected language setting.
            start = time.time()
            try:
                transcript = asr_backend.transcribe(audio, language=lang_local)
                text = transcript.text
                detected_lang = transcript.language
            except Exception as e:
                print(f"  Transcription failed: {e}")
                log_event(logging.ERROR, "transcription_failed", error=str(e))
                return
            elapsed = time.time() - start

            if not text:
                return

            session_count += 1
            print(f"  [{session_count}] ({detected_lang}, {duration:.1f}s audio, {elapsed:.1f}s transcribe)")
            if args.debug_transcript:
                print(f"  >>> {text}")
            log_event(
                logging.INFO,
                "transcription_succeeded",
                session=session_count,
                language=detected_lang,
                audio_duration_s=round(duration, 2),
                transcribe_duration_s=round(elapsed, 2),
            )
            try:
                append_transcript_log(
                    text,
                    language=detected_lang,
                    audio_duration_s=duration,
                    transcribe_duration_s=elapsed,
                    session=session_count,
                    source="daemon",
                )
            except Exception as e:
                log_event(logging.WARNING, "transcript_log_append_failed", error=str(e))

            remember_recent_transcription(text)

            if local_clipboard_enabled:
                try:
                    insert_text = format_for_insert(text, append_space=local_append_space)
                    copied = copy_to_clipboard(insert_text)
                    if copied and local_paste_enabled:
                        # Give compositor/focus a brief moment before sending paste hotkey.
                        time.sleep(0.2)
                        if not paste_from_clipboard(text=insert_text):
                            print("  [paste] failed (ensure ydotoold is running)")
                except Exception as e:
                    log_event(logging.WARNING, "clipboard_paste_pipeline_failed", error=str(e))
        finally:
            refresh_ready_tray()

    def hotkey_listener():
        pressed = set()
        sel = selectors.DefaultSelector()
        for kb in keyboards:
            sel.register(kb, selectors.EVENT_READ)

        try:
            while not shutdown_event.is_set():
                for key, mask in sel.select(timeout=0.25):
                    dev = key.fileobj
                    for event in dev.read():
                        if event.type != ecodes.EV_KEY:
                            continue

                        k = event.code
                        if event.value == 1:  # key down
                            pressed.add(k)
                        elif event.value == 0:  # key up
                            pressed.discard(k)

                        with hotkey_lock:
                            required_modifiers = list(hotkey_state["required_modifiers"])
                            trigger_key = hotkey_state["trigger_key"]
                        modifiers_ok = all(bool(pressed & MODIFIER_KEYS[mod]) for mod in required_modifiers)
                        has_trigger = trigger_key in pressed
                        combo_active = modifiers_ok and has_trigger

                        if combo_active and not recording:
                            start_recording()
                        elif not combo_active and recording:
                            threading.Thread(target=stop_recording, daemon=True).start()
        except PermissionError:
            print("Error: Cannot read keyboard. Add yourself to 'input' group:")
            print("  sudo usermod -aG input $USER")
            print("  Then log out and log back in.")
            if tray:
                tray.stop()
        except Exception as e:
            print(f"Hotkey listener error: {e}")
            log_event(logging.ERROR, "hotkey_listener_failed", error=str(e))
        finally:
            sel.close()
            for kb in keyboards:
                try:
                    kb.close()
                except Exception:
                    pass

    def refresh_tray_icons():
        nonlocal icons
        with settings_lock:
            selected = settings.get("icon_theme", "auto")
        icons, selected_variant, selected_mode = load_tray_icons(selected)
        log_event(
            logging.INFO,
            "tray_icons_reloaded",
            theme_variant=selected_variant,
            icon_theme_mode=selected_mode,
        )
        set_tray(tray_state.get("color", "green"), tray_state.get("title", f"Ready ({current_hotkey_label()})"))

    def validate_sound_assets():
        with settings_lock:
            enabled = bool(settings.get("sound_notifications"))
        if not enabled:
            return
        missing = []
        for name in ("start.wav", "stop.wav"):
            if not os.path.exists(os.path.join(SOUND_DIR, name)):
                missing.append(name)
        if missing:
            log_event(
                logging.WARNING,
                "sound_files_missing",
                expected_dir=SOUND_DIR,
                missing=missing,
            )

    def open_logs():
        log_path = os.path.join(STATE_DIR, "linuxflow.log")
        try:
            subprocess.Popen(["xdg-open", log_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"  Could not open logs: {e}")

    def open_transcript_log():
        open_transcript_log_viewer()

    def toggle_recording(icon_ref, item):
        with recording_lock:
            is_recording = recording
        if is_recording:
            threading.Thread(target=stop_recording, daemon=True).start()
        else:
            start_recording()

    def on_toggle_clipboard(icon_ref, item):
        with settings_lock:
            settings["clipboard_enabled"] = not settings["clipboard_enabled"]
            if not settings["clipboard_enabled"]:
                settings["paste_enabled"] = False
        save_settings()

    def on_toggle_paste(icon_ref, item):
        with settings_lock:
            settings["paste_enabled"] = not settings["paste_enabled"]
            if settings["paste_enabled"]:
                settings["clipboard_enabled"] = True
        save_settings()

    def on_toggle_sound_notifications(icon_ref, item):
        with settings_lock:
            settings["sound_notifications"] = not settings["sound_notifications"]
        save_settings()
        validate_sound_assets()

    def checked_autostart_login(item):
        return user_service_autostart_enabled()

    def on_toggle_autostart_login(icon_ref, item):
        enabling = not user_service_autostart_enabled()
        if set_user_service_autostart(enabling):
            notify(
                "LinuxFlow",
                "Starts automatically when you log in."
                if enabling
                else "Autostart disabled for next login.",
            )
            log_event(logging.INFO, "autostart_toggle", enabled=enabling)
        else:
            notify("LinuxFlow", "Could not change autostart (systemctl --user failed).")
        refresh_tray_menu()

    def set_model(model_name):
        def _handler(icon_ref, item):
            with settings_lock:
                settings["model"] = model_name
            save_settings()
            if restart_linuxflow_service():
                notify("LinuxFlow", f"Model changed to {model_name}. Service restarted.")
            else:
                notify("LinuxFlow", "Model changed, but service restart failed.")
        return _handler

    def set_language(language_name):
        def _handler(icon_ref, item):
            with settings_lock:
                settings["language"] = language_name
            save_settings()
        return _handler

    def set_hotkey(hotkey_name):
        def _handler(icon_ref, item):
            if not apply_hotkey(hotkey_name):
                print(f"  Unsupported hotkey: {hotkey_name}")
                return
            with settings_lock:
                settings["hotkey"] = hotkey_name
            save_settings()
        return _handler

    def set_icon_theme(icon_theme):
        def _handler(icon_ref, item):
            with settings_lock:
                settings["icon_theme"] = icon_theme
            save_settings()
            refresh_tray_icons()
        return _handler

    def on_set_custom_hotkey(icon_ref, item):
        def _runner():
            current = current_hotkey_label()
            raw = prompt_for_hotkey(current)
            if not raw:
                return
            normalized = normalize_hotkey_label(raw)
            if not apply_hotkey(normalized):
                try:
                    subprocess.Popen([
                        "notify-send",
                        "LinuxFlow",
                        f"Invalid hotkey: {raw}\nUse e.g. Ctrl+Alt+X",
                    ])
                except FileNotFoundError:
                    pass
                print(f"  Invalid hotkey input: {raw}")
                return
            with settings_lock:
                settings["hotkey"] = normalized
            save_settings()
            try:
                subprocess.Popen([
                    "notify-send",
                    "LinuxFlow",
                    f"Hotkey set to {normalized}",
                ])
            except FileNotFoundError:
                pass
        threading.Thread(target=_runner, daemon=True).start()

    def on_restart(icon_ref, item):
        restart_linuxflow_service()

    def on_open_logs(icon_ref, item):
        open_logs()

    def on_open_transcript_log(icon_ref, item):
        open_transcript_log()

    def on_clear_transcript_logs(icon_ref, item):
        try:
            clear_transcript_log_file()
        except OSError as e:
            log_event(logging.WARNING, "transcript_log_clear_failed", error=str(e))
            return
        with recent_transcripts_lock:
            recent_transcripts.clear()
        refresh_tray_menu()
        log_event(logging.INFO, "transcript_log_cleared")

    def on_quit(icon_ref, item):
        nonlocal recording, rec_process, rec_tmpfile
        shutdown_event.set()
        with recording_lock:
            recording = False
        if rec_process:
            stop_recorder_process(rec_process)
            rec_process = None
        if rec_tmpfile:
            try:
                os.unlink(rec_tmpfile)
            except OSError:
                pass
            rec_tmpfile = None
        try:
            asr_backend.close()
        except Exception as e:
            log_event(logging.WARNING, "asr_close_failed", error=str(e))
        remove_pid_file()
        icon_ref.stop()

    def setup(icon_ref):
        nonlocal tray
        tray = icon_ref
        tray.visible = True
        thawed = load_recent_transcript_texts_from_log(limit=RECENT_TRANSCRIPTIONS_MAX)
        if thawed:
            with recent_transcripts_lock:
                recent_transcripts.clear()
                for snippet in thawed:
                    recent_transcripts.appendleft(snippet)
            refresh_tray_menu()
        validate_sound_assets()
        refresh_ready_tray()
        write_pid_file()
        threading.Thread(target=hotkey_listener, daemon=True).start()
        print("\nRunning in background.")
        print(f"  Hold {current_hotkey_label()} to record, release to stop.")
        print("  Click tray icon for menu (left-click on some desktops), then choose Quit.\n")

    def is_recording_checked(item):
        with recording_lock:
            return recording

    def checked_setting(key):
        def _checked(item):
            latest = load_persistent_config()
            return bool(latest.get(key))
        return _checked

    def checked_model(model_name):
        def _checked(item):
            latest = load_persistent_config()
            return latest.get("model") == model_name
        return _checked

    def checked_language(language_name):
        def _checked(item):
            latest = load_persistent_config()
            return latest.get("language") == language_name
        return _checked

    def checked_hotkey(hotkey_name):
        def _checked(item):
            latest = load_persistent_config()
            return latest.get("hotkey") == hotkey_name
        return _checked

    def checked_icon_theme(icon_theme):
        def _checked(item):
            latest = load_persistent_config()
            return latest.get("icon_theme", "auto") == icon_theme
        return _checked

    def on_copy_recent_transcript(payload):
        normalized = payload.replace("\x00", "")

        def _handler(icon_ref, item):
            if not copy_to_clipboard(normalized):
                log_event(logging.WARNING, "recent_transcript_copy_failed")

        return _handler

    def build_tray_menu():
        with recent_transcripts_lock:
            snap = list(recent_transcripts)
        submenu_entries = []
        if snap:
            submenu_entries.extend(
                pystray.MenuItem(
                    format_transcript_tray_label(body),
                    on_copy_recent_transcript(body),
                )
                for body in snap
            )
        else:
            submenu_entries.append(pystray.MenuItem("(No recent yet)", None, enabled=False))
        submenu_entries.append(pystray.Menu.SEPARATOR)
        submenu_entries.append(pystray.MenuItem("Open Transcript Log", on_open_transcript_log))
        submenu_entries.append(pystray.MenuItem("Clear logs", on_clear_transcript_logs))

        submenu_transcriptions = pystray.Menu(*submenu_entries)

        return pystray.Menu(
            pystray.MenuItem("LinuxFlow", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start / Stop Recording", toggle_recording, checked=is_recording_checked, default=True),
            pystray.MenuItem("Restart Service", on_restart),
            pystray.MenuItem("Open Logs", on_open_logs),
            pystray.MenuItem(
                "Transcriptions",
                submenu_transcriptions,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Model",
                pystray.Menu(*[
                    pystray.MenuItem(model_name, set_model(model_name), checked=checked_model(model_name), radio=True)
                    for model_name in MODEL_OPTIONS
                ]),
            ),
            pystray.MenuItem(
                "Language",
                pystray.Menu(*[
                    pystray.MenuItem(language_name, set_language(language_name), checked=checked_language(language_name), radio=True)
                    for language_name in LANGUAGE_OPTIONS
                ]),
            ),
            pystray.MenuItem(
                "Hotkey",
                pystray.Menu(
                    *[
                        pystray.MenuItem(
                            hotkey_name,
                            set_hotkey(hotkey_name),
                            checked=checked_hotkey(hotkey_name),
                            radio=True,
                        )
                        for hotkey_name in HOTKEY_OPTIONS
                    ],
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Set Custom Hotkey...", on_set_custom_hotkey),
                ),
            ),
            pystray.MenuItem(
                "Icon Theme",
                pystray.Menu(*[
                    pystray.MenuItem(
                        theme_name.capitalize(),
                        set_icon_theme(theme_name),
                        checked=checked_icon_theme(theme_name),
                        radio=True,
                    )
                    for theme_name in ICON_THEME_OPTIONS
                ]),
            ),
            pystray.MenuItem(
                "Behaviors",
                pystray.Menu(
                    pystray.MenuItem("Copy to Clipboard", on_toggle_clipboard, checked=checked_setting("clipboard_enabled")),
                    pystray.MenuItem("Auto Paste", on_toggle_paste, checked=checked_setting("paste_enabled")),
                    pystray.MenuItem("Sound Notifications", on_toggle_sound_notifications, checked=checked_setting("sound_notifications")),
                    pystray.MenuItem("Start at Login", on_toggle_autostart_login, checked=checked_autostart_login),
                ),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        )

    def refresh_tray_menu():
        ic = tray
        if not ic:
            return

        def _gtk_apply():
            try:
                ic.menu = build_tray_menu()
                ic.update_menu()
            except Exception:
                pass
            return False

        if HAS_GLIB:
            GLib.idle_add(_gtk_apply)
        else:
            try:
                ic.menu = build_tray_menu()
                ic.update_menu()
            except Exception:
                pass

    def save_settings():
        with settings_lock:
            save_persistent_config(dict(settings))
        refresh_tray_menu()
        refresh_ready_tray()

    tray_menu_refresh = refresh_tray_menu

    tray_icon = pystray.Icon(
        "linuxflow",
        icon=icons["green"],
        title=f"LinuxFlow - Ready ({current_hotkey_label()})",
        menu=build_tray_menu(),
    )

    try:
        tray_icon.run(setup=setup)
    finally:
        try:
            asr_backend.close()
        except Exception as e:
            log_event(logging.WARNING, "asr_close_failed", error=str(e))
        remove_pid_file()


# ---------- Terminal mode ----------

def terminal_mode(args):
    """Interactive terminal mode: Enter start/stop, ESC exits (or discards clip while stopping)."""
    lang = None if args.language == "auto" else args.language

    print(f"Loading model '{args.model}'...")
    print("(First run downloads the model -- this may take a minute)\n")
    asr_backend = FasterWhisperBackend(
        model_name=args.model,
        device="cpu",
        compute_type="int8",
        sample_rate=SAMPLE_RATE,
        transcribe_tail_pad=TRANSCRIBE_TAIL_PAD,
        request_timeout_s=args.asr_timeout,
        max_retries=args.asr_retries,
    )
    print("Model loaded. Ready.\n")
    print("=" * 56)
    print("  LINUXFLOW — terminal mode")
    print("  Press Enter to start recording  ·  ESC to exit")
    print("  While recording: Enter to finish  ·  ESC discards clip")
    print("=" * 56)
    print("(PortAudio probe noise, ydotool, etc. goes to ~/.local/state/linuxflow/linuxflow.log)\n")

    session_count = 0

    try:
        while True:
            try:
                tty_prompt_enter_or_esc("Press Enter to start recording (ESC to exit)...\n")
            except _TTYEscExit:
                break

            print("  Recording… (speak now)")

            audio = record_audio(device_index=args.device)

            if audio is RECORD_CANCELLED:
                print("  Clip discarded.\n")
                continue

            if audio is None or len(audio) < SAMPLE_RATE * MIN_DURATION:
                print("  Too short, skipping.\n")
                continue

            duration = len(audio) / SAMPLE_RATE
            print(f"  Transcribing {duration:.1f}s of audio...")

            start = time.time()
            transcript = asr_backend.transcribe(audio, language=lang)
            text = transcript.text
            detected_lang = transcript.language
            elapsed = time.time() - start
            log_event(
                logging.INFO,
                "transcription_succeeded",
                session=session_count + 1,
                language=detected_lang,
                audio_duration_s=round(duration, 2),
                transcribe_duration_s=round(elapsed, 2),
            )

            if not text:
                print("  No speech detected.\n")
                continue

            session_count += 1
            print(f"\n  [{session_count}] ({detected_lang}, {elapsed:.1f}s)")
            print(f"  >>> {text}\n")
            try:
                append_transcript_log(
                    text,
                    language=detected_lang,
                    audio_duration_s=duration,
                    transcribe_duration_s=elapsed,
                    session=session_count,
                    source="terminal",
                )
            except Exception as e:
                log_event(logging.WARNING, "transcript_log_append_failed", error=str(e))

            if not args.no_clipboard:
                insert_text = format_for_insert(text)
                copied = copy_to_clipboard(insert_text)
                if copied:
                    print("  [clipboard] copied")
                    if not args.no_paste:
                        time.sleep(0.2)
                        if paste_from_clipboard(text=insert_text):
                            print("  [paste] done")
                        else:
                            log_event(
                                logging.WARNING,
                                "terminal_paste_fallback_failed",
                                hint="See log for ydotool/xdotool stderr; enable ydotoold or install xdotool if needed.",
                            )
                else:
                    log_event(
                        logging.WARNING,
                        "terminal_clipboard_copy_failed",
                        hint="wl-copy/xclip unavailable or timed out; use --no-clipboard or fix Wayland clipboard.",
                    )

            print()

    except KeyboardInterrupt:
        log_event(logging.INFO, "terminal_keyboard_interrupt")
        print("\n  Ctrl+C handled — shutting down cleanly.")
    except EOFError:
        log_event(logging.INFO, "terminal_eof")
    finally:
        print(f"\nDone. {session_count} transcriptions this session.")
        try:
            asr_backend.close()
        except Exception as e:
            log_event(logging.WARNING, "asr_close_failed", error=str(e))


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(description="LinuxFlow - Offline voice dictation for Linux")
    parser.add_argument(
        "--model", default="small",
        help="ASR model profile (default: small). Options: tiny, base, small, medium, large-v3-turbo"
    )
    parser.add_argument(
        "--language", default="en",
        help="Language code (default: en). Use 'auto' for auto-detection"
    )
    parser.add_argument(
        "--hotkey", default="Ctrl+Super+Z",
        choices=HOTKEY_OPTIONS,
        help="Daemon hotkey combo (default: Ctrl+Super+Z)"
    )
    parser.add_argument("--no-clipboard", action="store_true", help="Don't copy to clipboard")
    parser.add_argument("--no-paste", action="store_true", help="Don't auto-paste after copying to clipboard")
    parser.add_argument("--device", type=int, default=None, help="Audio input device index (terminal mode only)")
    parser.add_argument("--devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--config", action="store_true", help="Open interactive configuration menu and exit")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon with tray icon + hotkey")
    parser.add_argument("--asr-timeout", type=float, default=ASR_TIMEOUT_S, help="ASR request timeout in seconds")
    parser.add_argument("--asr-retries", type=int, default=ASR_RETRIES, help="Number of ASR retries after failure")
    parser.add_argument(
        "--debug-transcript",
        action="store_true",
        help="Daemon mode only: print transcript text to stdout/stderr for debugging (may expose dictated text in journal logs)",
    )
    parser.add_argument(
        "--open-transcript-log",
        action="store_true",
        help=f"Open the transcript history file ({TRANSCRIPT_LOG_PATH}) in the default viewer and exit",
    )
    parser.add_argument(
        "--transcript",
        type=int,
        metavar="N",
        default=None,
        help="Print the last N saved transcripts to stdout (body text only) and exit",
    )
    args = parser.parse_args()
    args._provided_flags = {
        token.split("=")[0]
        for token in sys.argv[1:]
        if token.startswith("--")
    }

    if args.devices:
        list_devices()
        sys.exit(0)

    if args.config:
        configure_settings_menu()
        sys.exit(0)

    if args.open_transcript_log:
        open_transcript_log_viewer()
        sys.exit(0)

    if args.transcript is not None:
        if args.transcript < 1:
            parser.error("--transcript N requires N >= 1")
        if args.daemon:
            parser.error("--transcript cannot be combined with --daemon")
        print_transcript_cli_recent(args.transcript)
        sys.exit(0)

    if args.daemon:
        if args.device is not None:
            parser.error("--device is not supported in --daemon mode; use terminal mode or remove --device.")
        daemon_mode(args)
    else:
        terminal_mode(args)


if __name__ == "__main__":
    main()
