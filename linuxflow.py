#!/usr/bin/env python3
"""
LinuxFlow - Offline voice dictation for Linux

Usage:
    python linuxflow.py                     # terminal mode (Enter to start/stop)
    python linuxflow.py --daemon            # background mode (tray icon + hotkey)
    python linuxflow.py --model tiny        # faster, lower quality
    python linuxflow.py --model base        # balanced
    python linuxflow.py --language auto     # auto-detect language
    python linuxflow.py --devices           # list audio devices

Daemon mode:
    Hold Ctrl+Super+Z to record, release to stop + transcribe.
    System tray icon shows status: green=ready, red=recording, orange=transcribing.
"""

import argparse
import configparser
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import signal
import os
import selectors
import subprocess
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

# Suppress noisy ALSA warnings on PipeWire systems
try:
    _asound = ctypes.cdll.LoadLibrary("libasound.so.2")
    _err_handler = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                     ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)(
        lambda *_: None
    )
    _asound.snd_lib_error_set_handler(_err_handler)
except OSError:
    pass

# Suppress GTK-CRITICAL warnings (cosmetic, from pystray on Wayland)
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
SOUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
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

logger = logging.getLogger("linuxflow")
if not logger.handlers:
    os.makedirs(STATE_DIR, exist_ok=True)
    handler = logging.StreamHandler()
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


def load_persistent_config():
    defaults = {
        "model": "small",
        "language": "en",
        "clipboard_enabled": True,
        "paste_enabled": True,
        "sound_notifications": False,
        "release_tail_buffer_s": 0.55,
        "hotkey": "Ctrl+Super+Z",
        "icon_theme": "auto",
    }
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
            if isinstance(loaded, dict):
                defaults.update(loaded)
    except FileNotFoundError:
        pass
    except Exception as e:
        log_event(logging.WARNING, "config_load_failed", error=str(e))

    if defaults["model"] not in MODEL_OPTIONS:
        defaults["model"] = "small"
    if defaults["language"] not in LANGUAGE_OPTIONS:
        defaults["language"] = "en"
    if not _is_parseable_hotkey(defaults["hotkey"]):
        defaults["hotkey"] = "Ctrl+Super+Z"
    if defaults["icon_theme"] not in ICON_THEME_OPTIONS:
        defaults["icon_theme"] = "auto"
    try:
        defaults["release_tail_buffer_s"] = float(defaults.get("release_tail_buffer_s", 0.55))
    except (TypeError, ValueError):
        defaults["release_tail_buffer_s"] = 0.55
    defaults["release_tail_buffer_s"] = max(0.1, min(1.5, defaults["release_tail_buffer_s"]))
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
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)


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
        print("7) Exit Config")
        print("Choose an item number:")

    while True:
        _print_header()
        choice = _prompt_choice(7)
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
    p = pyaudio.PyAudio()
    print("Audio input devices:")
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            marker = " <-- default" if i == p.get_default_input_device_info()["index"] else ""
            print(f"  [{i}] {info['name']} ({info['maxInputChannels']}ch){marker}")
    p.terminate()


def record_audio(device_index=None):
    """Record audio from microphone until Enter is pressed. Returns float32 numpy array."""
    p = pyaudio.PyAudio()

    kwargs = dict(
        format=FORMAT,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )
    if device_index is not None:
        kwargs["input_device_index"] = device_index

    try:
        stream = p.open(**kwargs)
    except OSError as e:
        print(f"  Error opening mic: {e}")
        p.terminate()
        return None

    frames = []
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

    input("  Press Enter to stop recording...\n")

    is_recording = False
    thread.join(timeout=2)

    stream.stop_stream()
    stream.close()
    p.terminate()

    if not frames:
        return None

    audio = np.frombuffer(b"".join(frames), dtype=np.int16)
    audio = audio.astype(np.float32) / 32768.0
    return audio


def format_for_insert(text):
    """Prepare transcript text for clipboard/paste insertion."""
    return text.rstrip() + " "


def copy_to_clipboard(text):
    """Copy text to clipboard (Wayland: wl-copy, fallback: xclip)."""
    for cmd in [["wl-copy", "--"], ["xclip", "-selection", "clipboard"]]:
        try:
            subprocess.run(cmd, input=text, text=True, timeout=5, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return False


def type_text(text):
    """Type text into focused window using ydotool."""
    try:
        subprocess.run(
            ["ydotool", "type", "--key-delay", "3", "--", text],
            timeout=30,
            check=True,
        )
        return True
    except FileNotFoundError:
        return False
    except subprocess.CalledProcessError:
        return False


def paste_from_clipboard(text=None):
    """Paste clipboard content into focused window, fallback to typing text."""
    # Prefer ydotool (Wayland-friendly), fallback to xdotool (X11).
    try:
        subprocess.run(
            ["ydotool", "key", "--key-delay", "3", "29:1", "47:1", "47:0", "29:0"],  # Ctrl+V
            timeout=5,
            check=True,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    try:
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], timeout=5, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

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
        dev = InputDevice(path)
        caps = dev.capabilities().get(ecodes.EV_KEY, [])
        if ecodes.KEY_A in caps and ecodes.KEY_SPACE in caps:
            keyboards.append(dev)
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
            candidate = os.path.join(icon_root, f"{variant}-{state}-{size}.png")
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
            local_language = settings["language"]
        lang_local = None if local_language == "auto" else local_language

        # Use the currently selected language setting.
        start = time.time()
        try:
            transcript = asr_backend.transcribe(audio, language=lang_local)
            text = transcript.text
            detected_lang = transcript.language
        except Exception as e:
            print(f"  Transcription failed: {e}")
            log_event(logging.ERROR, "transcription_failed", error=str(e))
            refresh_ready_tray()
            return
        elapsed = time.time() - start

        if not text:
            refresh_ready_tray()
            return

        session_count += 1
        print(f"  [{session_count}] ({detected_lang}, {duration:.1f}s audio, {elapsed:.1f}s transcribe)")
        print(f"  >>> {text}")
        log_event(
            logging.INFO,
            "transcription_succeeded",
            session=session_count,
            language=detected_lang,
            audio_duration_s=round(duration, 2),
            transcribe_duration_s=round(elapsed, 2),
        )

        if local_clipboard_enabled:
            insert_text = format_for_insert(text)
            copied = copy_to_clipboard(insert_text)
            if copied and local_paste_enabled:
                # Give compositor/focus a brief moment before sending paste hotkey.
                time.sleep(0.2)
                if not paste_from_clipboard(text=insert_text):
                    print("  [paste] failed (ensure ydotoold is running)")

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

    def save_settings():
        with settings_lock:
            save_persistent_config(dict(settings))
        if tray:
            try:
                tray.update_menu()
            except Exception:
                pass
        refresh_ready_tray()

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

    def restart_service():
        try:
            result = subprocess.run(
                ["systemctl", "--user", "restart", "linuxflow.service"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return result.returncode == 0
        except Exception as e:
            print(f"  Restart failed: {e}")
            return False

    def open_logs():
        log_path = os.path.join(STATE_DIR, "linuxflow.log")
        try:
            subprocess.Popen(["xdg-open", log_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"  Could not open logs: {e}")

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

    def set_model(model_name):
        def _handler(icon_ref, item):
            with settings_lock:
                settings["model"] = model_name
            save_settings()
            if restart_service():
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
        restart_service()

    def on_open_logs(icon_ref, item):
        open_logs()

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
        remove_pid_file()
        icon_ref.stop()

    def setup(icon_ref):
        nonlocal tray
        tray = icon_ref
        tray.visible = True
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

    tray_icon = pystray.Icon(
        "linuxflow",
        icon=icons["green"],
        title=f"LinuxFlow - Ready ({current_hotkey_label()})",
        menu=pystray.Menu(
            pystray.MenuItem("LinuxFlow", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start / Stop Recording", toggle_recording, checked=is_recording_checked, default=True),
            pystray.MenuItem("Restart Service", on_restart),
            pystray.MenuItem("Open Logs", on_open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Copy to Clipboard", on_toggle_clipboard, checked=checked_setting("clipboard_enabled")),
            pystray.MenuItem("Auto Paste", on_toggle_paste, checked=checked_setting("paste_enabled")),
            pystray.MenuItem("Sound Notifications", on_toggle_sound_notifications, checked=checked_setting("sound_notifications")),
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
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        ),
    )

    try:
        tray_icon.run(setup=setup)
    finally:
        remove_pid_file()


# ---------- Terminal mode ----------

def terminal_mode(args):
    """Interactive terminal mode with Enter key to start/stop."""
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
    print("=" * 50)
    print("  LINUXFLOW")
    print("  Press Enter to START recording")
    print("  Press Enter again to STOP")
    print("  Ctrl+C to quit")
    print("=" * 50)
    print()

    session_count = 0

    try:
        while True:
            input("Press Enter to start recording...")
            print("  Recording... (speak now)")

            audio = record_audio(device_index=args.device)

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
                            print("  [paste] failed (ensure ydotoold is running)")

            print()

    except KeyboardInterrupt:
        print(f"\n\nDone. {session_count} transcriptions this session.")


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
    parser.add_argument("--device", type=int, default=None, help="Audio input device index")
    parser.add_argument("--devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--config", action="store_true", help="Open interactive configuration menu and exit")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon with tray icon + hotkey")
    parser.add_argument("--asr-timeout", type=float, default=ASR_TIMEOUT_S, help="ASR request timeout in seconds")
    parser.add_argument("--asr-retries", type=int, default=ASR_RETRIES, help="Number of ASR retries after failure")
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

    if args.daemon:
        daemon_mode(args)
    else:
        terminal_mode(args)


if __name__ == "__main__":
    main()
