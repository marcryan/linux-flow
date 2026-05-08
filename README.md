# LinuxFlow

Free, offline, open-source alternative to [WisprFlow](https://wisprflow.ai) for Linux. Speak anywhere, get clean text - no cloud, no subscription, no data leaves your machine.

**Hold `Ctrl+Super+Z`, speak, release - text is transcribed, copied, and auto-pasted into your focused app.**

## Quick Start (One Command)

```bash
git clone https://github.com/marcryan/linux-flow.git
cd linux-flow
./install.sh
```

Installer actions:

- installs distro dependencies (`apt`, `dnf`, `pacman`)
- creates `~/.local/share/linuxflow` with an isolated venv
- installs user service: `~/.config/systemd/user/linuxflow.service`
- installs launcher: `~/.local/share/applications/linuxflow.desktop`
- adds your user to the `input` group so LinuxFlow can read keyboard devices for the global hotkey (full effect after logout/login; until then `start.sh` may use `sg input`)

### Security

LinuxFlow reads `/dev/input/event*` (via evdev) for the global hotkey. Older installers used a broad `udev` `uaccess` rule that granted access to **every** keyboard device for your login session — including sandboxed apps — which widened attack surface. Current installs use the standard **`input` group** instead (same broad device access as other input tooling, but no extra system-wide ACL beyond that pattern).

If install added you to `input` and you uninstall, run `sudo gpasswd -d "$USER" input` if you want to leave the group (re-login afterward).

## How It Works

```
Hold Ctrl+Super+Z → mic records → release → faster-whisper transcribes → clipboard + auto-paste
```

Two modes:


| Mode         | How to run                     | How it works                                          |
| ------------ | ------------------------------ | ----------------------------------------------------- |
| **Daemon**   | `python linuxflow.py --daemon` | System tray icon + global hotkey. No terminal needed. |
| **Terminal** | `python linuxflow.py`          | Press Enter to start/stop. Good for testing.          |


## Features

- **100% offline** - all processing happens locally, no internet needed
- **Global hotkey** - `Ctrl+Super+Z` works from any app (browser, editor, chat, etc.)
- **System tray icon** - theme-aware light/dark assets (idle/recording/transcribing)
- **Tray settings menu** - right-click tray icon to change model, language, hotkey, and behavior toggles
- **Clipboard + auto-paste** - transcription is copied and then pasted into the focused app
- **Optional hotkey sounds** - play custom start/stop sounds on key press/release
- **Wayland + X11** - works on both via evdev + parecord
- **100+ languages** - auto-detection or specify with `--language`
- **Multiple models** - trade speed for accuracy based on your hardware
- **No completion toasts** - no desktop notification popups after transcription
- **Short utterance tuning** - better one-word capture and reduced last-word clipping

## RAM & Performance

Tested on Intel i5-10210U (4 cores), 16GB RAM, Fedora 43.


| Model            | RAM Usage      | Transcription Speed | Quality   | Best For                         |
| ---------------- | -------------- | ------------------- | --------- | -------------------------------- |
| `tiny`           | ~300 MB        | ~0.7s for 10s audio | Basic     | Quick notes, fast hardware       |
| `base`           | ~500 MB        | ~1.2s for 10s audio | OK        | Everyday use on low-end hardware |
| `small`          | ~625 MB        | ~4s for 25s audio   | Good      | **Recommended for most users**   |
| `medium`         | ~2.5 GB        | ~8s for 25s audio   | Great     | When accuracy matters            |
| `large-v3-turbo` | ~2.5 GB (INT8) | ~6s for 25s audio   | Near-best | 16GB+ RAM systems                |


Models download automatically on first run. The `small` model (~500MB download) is the default.

## Installation

### Prerequisites

- Linux (tested on Fedora 43, should work on Ubuntu/Arch/etc.)
- Python 3.10+
- PipeWire or PulseAudio (default on modern distros)
- A microphone

### Step 1: Run installer

```bash
./install.sh
```

### Step 2: Verify service

```bash
systemctl --user status linuxflow.service
journalctl --user -u linuxflow -f
```

Current `requirements.txt`:

```txt
faster-whisper>=1.0.0
PyAudio>=0.2.13
numpy>=1.24.0
evdev>=1.7.0
pystray>=0.19.0
Pillow>=10.0.0
```

### Step 3: Run

```bash
systemctl --user restart linuxflow.service
```

The installer adds your account to the `input` group if needed. After the first install, **log out and back in** once so your session picks up the new group membership everywhere (until then, the service launcher may still start via `sg input`).

## Usage

### Daemon Mode (recommended)

```bash
python linuxflow.py --daemon                    # default (small model)
python linuxflow.py --config                    # interactive settings menu
python linuxflow.py --daemon --model tiny       # faster, less accurate
python linuxflow.py --daemon --model medium     # slower, more accurate
python linuxflow.py --daemon --language auto    # auto-detect language
python linuxflow.py --daemon --language hi      # Hindi
python linuxflow.py --daemon --no-clipboard     # don't copy to clipboard
python linuxflow.py --daemon --no-paste         # don't auto-paste after copy
```

If you installed into `venv`, run with:

```bash
./venv/bin/python linuxflow.py --daemon
```

Then from any app: **hold `Ctrl+Super+Z`**, speak, **release**.

Click the tray icon to manage (on some desktops, this is left-click instead of right-click):

- start/stop recording, restart service, open logs, quit
- clipboard/paste/sound notification toggles
- model, language, and hotkey (persisted in `~/.config/linuxflow/config.json`)
- force tray icon theme with `icon_theme` in `~/.config/linuxflow/config.json`: `auto`, `light`, or `dark`
- sound files (when enabled): `sounds/start.wav` and `sounds/stop.wav`
- trailing insertion space is configurable via `append_space` in `~/.config/linuxflow/config.json` (default `true`)
- release tail capture is configurable via `release_tail_buffer_s` in `~/.config/linuxflow/config.json` (default `0.55`)
- `Hotkey -> Set Custom Hotkey...` opens a dialog (kdialog/zenity) to type any combo
  - Modifiers: `Ctrl`, `Alt`, `Shift`, `Super`
  - Triggers: `A-Z`, `0-9`, `F1-F12`, `Space`, `Tab`, `Enter`, `Esc`, `CapsLock`
  - Examples: `Ctrl+Alt+X`, `Ctrl+Super+F9`, `Ctrl+Shift+Space`, `CapsLock`

### Terminal Mode

```bash
python linuxflow.py                             # interactive mode
python linuxflow.py --model tiny                # use tiny model
```

Press `Enter` to start recording, `Enter` again to stop.

### Terminal Config Menu

Use `python linuxflow.py --config` for a numbered settings menu in terminal. It supports:

- model, language, hotkey (including `CapsLock`)
- copy to clipboard, auto paste, sound notifications
- `Exit Config`

When a change requires daemon reload (for example model/hotkey/language), the menu prompts to restart `linuxflow.service` immediately.

### All Options


| Flag              | Description                                                                      |
| ----------------- | -------------------------------------------------------------------------------- |
| `--daemon`        | Run as background daemon with tray icon + hotkey                                 |
| `--model MODEL`   | ASR model profile: `tiny`, `base`, `small` (default), `medium`, `large-v3-turbo` |
| `--language LANG` | Language code (`en`, `hi`, `es`, etc.) or `auto` for detection                   |
| `--no-clipboard`  | Don't copy to clipboard                                                          |
| `--no-paste`      | Don't auto-paste after copying to clipboard                                      |
| `--config`        | Open interactive terminal menu to configure tray settings                        |
| `--device N`      | Use specific audio input device (terminal mode only; see `--devices`)            |
| `--devices`       | List available audio input devices                                               |
| `--asr-timeout S` | ASR timeout in seconds (default: `30`)                                           |
| `--asr-retries N` | Retry ASR N times after failure (default: `1`)                                   |
| `--debug-transcript` | Daemon only: print transcript text to stdout/stderr for debugging (may expose text in `journalctl`) |

By default, daemon mode does not print dictated transcript text to logs/journal. Use `--debug-transcript` only when you explicitly want local debugging output.


## Auto-Start on Login (systemd)

`install.sh` enables this automatically:

```bash
systemctl --user enable --now linuxflow.service
```

### Service Commands


| Command                              | What it does   |
| ------------------------------------ | -------------- |
| `systemctl --user start linuxflow`   | Start          |
| `systemctl --user stop linuxflow`    | Stop           |
| `systemctl --user restart linuxflow` | Restart        |
| `systemctl --user status linuxflow`  | Check status   |
| `journalctl --user -u linuxflow -f`  | View live logs |


### Manage Service Without a Console Window

```bash
systemctl --user enable --now linuxflow.service   # start now + auto-start on login
systemctl --user restart linuxflow.service        # reload after code/config changes
systemctl --user stop linuxflow.service           # stop service
```

## Uninstall

```bash
./uninstall.sh
```

## Architecture

```
┌─────────────┐     ┌──────────┐     ┌───────────────┐     ┌──────────┐
│ evdev        │────>│ parecord │────>│ faster-whisper │────>│ wl-copy  │
│ (hotkey)     │     │ (mic)    │     │ (STT)          │     │ (clipboard)│
└─────────────┘     └──────────┘     └───────────────┘     └──────────┘
```


| Component                | Tool                     | Why                                                |
| ------------------------ | ------------------------ | -------------------------------------------------- |
| Global hotkey            | `evdev`                  | Works on Wayland + X11 (kernel-level)              |
| Audio capture            | `parecord` / `pw-record` | Works on PulseAudio and PipeWire setups            |
| Speech-to-text           | `faster-whisper`         | 4x faster than OpenAI Whisper, INT8 quantization   |
| Clipboard                | `wl-copy` / `xclip`      | Wayland-first with X11 fallback                    |
| System tray              | `pystray`                | Cross-desktop (GNOME, KDE, etc.)                   |
| Auto-paste/type fallback | `ydotool` / `xdotool`    | Paste first, then direct typing fallback if needed |


## vs WisprFlow


|            | WisprFlow                     | LinuxFlow            |
| ---------- | ----------------------------- | -------------------- |
| Price      | $15/month                     | Free                 |
| Privacy    | Cloud (audio sent to servers) | 100% local           |
| Internet   | Required                      | Not needed           |
| RAM        | ~800 MB (idle)                | ~300-625 MB (active) |
| Platforms  | Mac, Windows, iOS, Android    | Linux                |
| AI cleanup | Yes (Flow mode)               | Coming soon (Ollama) |
| Languages  | 100+                          | 100+                 |


## Troubleshooting

**"No keyboard found"** - Re-run `./install.sh` (it adds you to `input`), or manually: `sudo usermod -aG input "$USER"` and log out/in. Quick workaround: `newgrp input` before running.

**No tray icon on GNOME** - Install and enable the AppIndicator extension:

```bash
sudo dnf install gnome-shell-extension-appindicator
# Then enable "AppIndicator and KStatusNotifierItem Support" in GNOME Extensions app
```

**Tray icon/menu missing on Arch/CachyOS** - Install an AppIndicator library, then restart LinuxFlow:

```bash
sudo pacman -S --needed libayatana-appindicator
systemctl --user restart linuxflow.service
```

**ALSA/GTK warnings in logs** - LinuxFlow suppresses common ALSA/GTK noise by default. Set `LINUXFLOW_SUPPRESS_ALSA_GTK_WARNINGS=0` to re-enable raw native warnings for debugging.

**Where logs are written** - Runtime logs are written to `~/.local/state/linuxflow/linuxflow.log` (rotated, with backups).

**Wrong language detected** - Use `--language en` (or your language code) instead of auto-detect.

**High latency** - Switch to a smaller model: `--model tiny` or `--model base`.

**Clipboard not copying** - Install one of: `wl-clipboard` (Wayland) or `xclip` (X11).

**Paste not working on Wayland** - Ensure `ydotool` service is running:

```bash
sudo systemctl enable --now ydotool.service
```

**Short one-word dictation misses** - This build already includes shorter minimum duration + tail buffering + transcription tail padding to reduce last-word drops.

## Future Plans

- LLM text cleanup via Ollama (remove filler words, fix grammar - "Flow mode")
- Custom hotkey configuration
- Per-app tone adjustment

## License

MIT