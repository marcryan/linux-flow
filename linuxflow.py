#!/usr/bin/env python3
"""
LinuxFlow - Offline voice dictation for Linux

Usage:
    python linuxflow.py                     # terminal mode (Enter to start/stop)
    python linuxflow.py --daemon            # background mode (tray icon + hotkey)
    python linuxflow.py --model tiny        # faster, lower quality
    python linuxflow.py --model base        # balanced
    python linuxflow.py --language auto     # auto-detect language
    python linuxflow.py --no-save           # skip Obsidian save
    python linuxflow.py --type              # auto-type via ydotool
    python linuxflow.py --devices           # list audio devices

Daemon mode:
    Hold Ctrl+Super+Z to record, release to stop + transcribe.
    System tray icon shows status: green=ready, red=recording, orange=transcribing.
"""

import argparse
import ctypes
import datetime
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
OBSIDIAN_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "linuxflow", "transcripts")
STATE_DIR = os.path.join(os.path.expanduser("~"), ".local", "state", "linuxflow")
PID_FILE = os.path.join(STATE_DIR, "linuxflow.pid")
MIN_DURATION = 0.2  # seconds - allow short one-word utterances
POST_RELEASE_BUFFER = 0.35  # seconds - capture trailing phonemes after key release
TRANSCRIBE_TAIL_PAD = 0.25  # seconds - append silence to preserve final token
ASR_TIMEOUT_S = 30.0
ASR_RETRIES = 1

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


def save_to_obsidian(text, save_dir):
    """Append transcript to today's daily note in Obsidian vault. Returns filepath."""
    os.makedirs(save_dir, exist_ok=True)

    now = datetime.datetime.now()
    filename = now.strftime("%Y-%m-%d") + ".md"
    filepath = os.path.join(save_dir, filename)

    time_str = now.strftime("%H:%M:%S")

    if os.path.exists(filepath):
        with open(filepath, "a") as f:
            f.write(f"- **{time_str}** — {text}\n")
    else:
        content = (
            f"---\n"
            f"date: {now.strftime('%Y-%m-%d')}\n"
            f"type: voice-transcripts\n"
            f"---\n\n"
            f"# Transcripts - {now.strftime('%B %d, %Y')}\n\n"
            f"- **{time_str}** — {text}\n"
        )
        with open(filepath, "w") as f:
            f.write(content)

    return filepath


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


def daemon_mode(args):
    """Run as background daemon with system tray and global hotkey."""
    if not HAS_EVDEV:
        print("Error: evdev not installed. Run: pip install evdev")
        sys.exit(1)
    if not HAS_TRAY:
        print("Error: pystray/Pillow not installed. Run: pip install pystray Pillow")
        sys.exit(1)

    lang = None if args.language == "auto" else args.language

    print(f"Loading model '{args.model}'...")
    asr_backend = FasterWhisperBackend(
        model_name=args.model,
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

    # Hotkey: Ctrl+Super+Z
    CTRL_KEYS = {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL}
    SUPER_KEYS = {ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA}
    TRIGGER_KEY = ecodes.KEY_Z

    # Shared state
    recording = False
    recording_lock = threading.Lock()
    rec_process = None
    rec_tmpfile = None
    tray = None
    session_count = 0
    hotkey_label = "Ctrl+Super+Z"
    shutdown_event = threading.Event()

    # Pre-create icons to avoid GTK calls from threads
    icon_green = make_icon("green")
    icon_red = make_icon("red")
    icon_orange = make_icon("orange")
    icons = {"green": icon_green, "red": icon_red, "orange": icon_orange}

    def set_tray(color, title):
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
                set_tray("green", f"Ready ({hotkey_label})")
                return

        set_tray("red", "Recording...")

    def stop_recording():
        nonlocal recording, rec_process, rec_tmpfile, session_count
        with recording_lock:
            if not recording:
                return
            recording = False

        if rec_process:
            # Small tail buffer helps avoid clipping final syllable/word.
            time.sleep(POST_RELEASE_BUFFER)
            stop_recorder_process(rec_process)
            rec_process = None

        set_tray("orange", "Transcribing...")

        # Read the recorded wav file
        if not rec_tmpfile:
            set_tray("green", f"Ready ({hotkey_label})")
            return
        try:
            import wave
            with wave.open(rec_tmpfile, "rb") as wf:
                raw = wf.readframes(wf.getnframes())
                if len(raw) < SAMPLE_RATE * MIN_DURATION * 2:  # 2 bytes per sample
                    set_tray("green", f"Ready ({hotkey_label})")
                    return
                audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception as e:
            print(f"  Error reading audio: {e}")
            log_event(logging.ERROR, "recording_read_failed", error=str(e))
            set_tray("green", f"Ready ({hotkey_label})")
            return
        finally:
            try:
                os.unlink(rec_tmpfile)
            except OSError:
                pass
            rec_tmpfile = None

        duration = len(audio) / SAMPLE_RATE

        start = time.time()
        try:
            transcript = asr_backend.transcribe(audio, language=lang)
            text = transcript.text
            detected_lang = transcript.language
        except Exception as e:
            print(f"  Transcription failed: {e}")
            log_event(logging.ERROR, "transcription_failed", error=str(e))
            set_tray("green", f"Ready ({hotkey_label})")
            return
        elapsed = time.time() - start

        if not text:
            set_tray("green", f"Ready ({hotkey_label})")
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

        if not args.no_clipboard:
            insert_text = format_for_insert(text)
            copied = copy_to_clipboard(insert_text)
            if copied and not args.no_paste:
                # Give compositor/focus a brief moment before sending paste hotkey.
                time.sleep(0.2)
                if not paste_from_clipboard(text=insert_text):
                    print("  [paste] failed (ensure ydotoold is running)")

        if args.auto_type:
            type_text(text)

        if not args.no_save:
            saved_path = save_to_obsidian(text, args.save_dir)
            print(f"  [obsidian] {os.path.basename(saved_path)}")

        set_tray("green", f"Ready ({hotkey_label})")

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

                        has_ctrl = bool(pressed & CTRL_KEYS)
                        has_super = bool(pressed & SUPER_KEYS)
                        has_trigger = TRIGGER_KEY in pressed
                        combo_active = has_ctrl and has_super and has_trigger

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
        write_pid_file()
        threading.Thread(target=hotkey_listener, daemon=True).start()
        print("\nRunning in background.")
        print(f"  Hold {hotkey_label} to record, release to stop.")
        print("  Right-click tray icon to quit.\n")

    tray_icon = pystray.Icon(
        "linuxflow",
        icon=make_icon("green"),
        title=f"LinuxFlow - Ready ({hotkey_label})",
        menu=pystray.Menu(
            pystray.MenuItem("LinuxFlow", None, enabled=False),
            pystray.MenuItem(f"Model: {args.model}", None, enabled=False),
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

            if args.auto_type:
                if type_text(text):
                    print("  [ydotool] typed")
                else:
                    print("  [ydotool] not available -- install package: ydotool")

            if not args.no_save:
                path = save_to_obsidian(text, args.save_dir)
                print(f"  [obsidian] {os.path.basename(path)}")

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
    parser.add_argument("--save-dir", default=OBSIDIAN_DIR, help="Directory to save transcripts")
    parser.add_argument("--no-save", action="store_true", help="Don't save to Obsidian")
    parser.add_argument("--no-clipboard", action="store_true", help="Don't copy to clipboard")
    parser.add_argument("--no-paste", action="store_true", help="Don't auto-paste after copying to clipboard")
    parser.add_argument("--type", dest="auto_type", action="store_true", help="Auto-type via ydotool")
    parser.add_argument("--device", type=int, default=None, help="Audio input device index")
    parser.add_argument("--devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon with tray icon + hotkey")
    parser.add_argument("--asr-timeout", type=float, default=ASR_TIMEOUT_S, help="ASR request timeout in seconds")
    parser.add_argument("--asr-retries", type=int, default=ASR_RETRIES, help="Number of ASR retries after failure")
    args = parser.parse_args()

    if args.devices:
        list_devices()
        sys.exit(0)

    if args.daemon:
        daemon_mode(args)
    else:
        terminal_mode(args)


if __name__ == "__main__":
    main()
