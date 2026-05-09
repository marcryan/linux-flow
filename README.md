# LinuxFlow

Free, offline, open-source alternative to [WisprFlow](https://wisprflow.ai) for Linux. Speak in any focused window, release the hotkey, and get transcribed text on the clipboard—with optional paste into the app. Nothing is sent to the cloud unless you explicitly add that later yourself.

# **Main Features**

- **Free!!** - This doesn't cost you a thing
- **Private** - 100% offline all processing happens locally, no internet needed
- **Global hotkey** - Use the default `Ctrl+Super+Z` or set your own
- **System tray icon & submenus** - Configure behavior of the app without the terminal
- **Terminal mode** - enables transcription of text to a file and/or clipboard
- **Clipboard + auto-paste** -  Works from any app (browser, editor, chat, etc.) transcription is copied and then pasted into the focused app
- **Wayland + X11** - works on both via evdev + parecord
- **Multiple languages** - auto-detection or specify with `--language`
- **Multiple models** - trade speed for accuracy based on your hardware

---

# Quick start

```bash
git clone https://github.com/marcryan/linux-flow.git
cd linux-flow
./install.sh
echo "Log out and back in, then run from system menu or terminal"
```

After install, **`linuxflow`** runs under your user systemd session (see logs below). The installer adds you to the **`input`** group for global hotkeys (evdev); **log out and back in** (or reboot) so that takes effect without fallbacks.

---

# Usage

## From KDE/Plasma

#### Basic Use

1. Click into text field in desired app
2. Hold Hotkey (Ctrl+Super+Z)
3. mic records
4. release hotkey
5. faster-whisper transcribes
6. text added to clipboard & auto-pasted into app

### Tray icon meanings (default theme)

The tray uses PNGs named **`idle`** / **`rec`** / **`pro`** (light or dark variant). In the default set they read as:

- **White**: idle / ready
- **Green**: recording
- **Orange**: transcribing (processing)

#### Options

LinuxFlow runs in the system tray. Clicking on the icon provides a context menu of configurable options.

- **Start/stop** recording: a  manual toggle which mirrors the hardware hotkey pipeline
- **Restart service**, **Open logs**, **Quit Application**
- **Transcriptions** submenu: 
  - copies of the last few transcripts (click to copy to clipboard), 
  - **open transcript Markdown logs**, 
  - **clear logs**
- **Models** submenu:
  - Tiny
  - Base
  - Small (default)
  - Medium
  - Large-v3-turbo
- **Language** submenu
  - Choose from several supported languages
- **Hotkey** submenu:
  - Default (Ctrl+Super+Z)
  - Several Built in Options
  - Choose your own hotkey
- **Behaviors** submenu:
  - Toggle clipboard copy
  - Toggle Auto Paste
  - Toggle Sound Notifications
- **Icons** submenu:
  - Forces refresh of icons or match system theme

## From Terminal

After **`./install.sh`** you get **`~/.local/bin/linuxflow`**, which runs the installed copy with **`~/.local/share/linuxflow/venv`** — from **any directory**:

#### Start Recording

```bash
linuxflow
```

#### Configure Options

```bash
linuxflow --config
```

#### Select device

```bash
linuxflow --devices
```

#### Show Transcriptions

```bash
linuxflow --transcript N

# Replace N with number of recordings to display #
```

**Do not** run **`python linuxflow.py`** from the **git clone** unless `python` is that clone’s venv: your default interpreter is usually **system** Python → **`No module named 'pyaudio'`**. Use **`linuxflow`** instead.

- **Fish:** installer adds **`~/.config/fish/conf.d/linuxflow-path.fish`**. Open a **new terminal** or **`exec fish`**, then **`linuxflow`**.
- **Bash/zsh:** add **`PATH`** if needed: **`export PATH="$HOME/.local/bin:$PATH"`**
- Fallback: **`~/.local/share/linuxflow/venv/bin/python ~/.local/share/linuxflow/linuxflow.py`**



---

# Installation

Follow the quick set-up instructions above.

## What `./install.sh` does

Installs distro packages where it can (**pacman**, **apt**, or **dnf**), then:

- Copies the app into **`~/.local/share/linuxflow`**
- Creates a dedicated venv at **`~/…/linuxflow/venv`** and installs Python deps from **`requirements.txt`**
- Writes a systemd user unit at **`~/.config/systemd/user/linuxflow.service`**
- Installs **`~/.local/bin/linuxflow`** and, if **`fish`** is installed, **`~/.config/fish/conf.d/linuxflow-path.fish`**, so **`~/.local/bin`** is on **`PATH`** in new Fish sessions
- Adds **`~/.local/share/applications/linuxflow.desktop`** with **`Icon=`** set to **`icons/linuxflowicon.svg`** (PNG fallback if that file is missing)
- Adds your user to the Unix **`input`** group so the process can open **`/dev/input/event*`** for global hotkeys
- Removes a legacy **`udev`/`uaccess`** keyboard rule (**`70-linuxflow-input.rules`**), if present, in favor of the **`input`** group approach

HF / model cache for faster-whisper is directed to **`~/.local/state/linuxflow/hf-cache`** via the unit’s **`HF_HOME`** environment variable.

### Security note

Reading keyboard devices (**`evdev`**) is inherently privileged compared to sandboxed GUI apps—the tradeoff enables a global shortcut on Wayland and X11. Older installers used an overly broad **`udev`/`uaccess`** rule that could expose **all** keyboards to session seat ACLs beyond what you get from **`input`** membership alone. Current installs rely on **`input`** (like many other utilities) plus optional **`sg input`** fallback in **`start.sh`** until your session sees the group.

Leaving **`input`** after uninstall: `./uninstall.sh` prints **`gpasswd`** instructions; you stay in **`input`** until you explicitly remove yourself and re-login.

## How it runs

Typical daemon flow:

```
Global hotkey (evdev)
    → parecord OR pw-record (16 kHz WAV to a temp file)
    → FasterWhisperBackend (spawned worker, CPU int8, optional VAD tail padding)
    → optional transcript Markdown log
    → wl-copy/xclip (+ ydotool/xdotool typing fallback paste)
```

**Terminal mode** is different by design: it records with **PyAudio** streams (**Enter** to stop recording, **ESC** to exit or discard the current clip), not `parecord`/`pw-record`. Use it for microphone testing and scripted use.

## Where configuration and data live


| Path                                         | Purpose                                                    |
| -------------------------------------------- | ---------------------------------------------------------- |
| `~/.config/linuxflow/config.json`            | Persistent daemon settings (validated keys only)           |
| `~/.local/state/linuxflow/linuxflow.log`     | Rotating application logs                                  |
| `~/.local/state/linuxflow/transcript_log.md` | Append-only Markdown history (daemon + terminal successes) |
| `~/.local/state/linuxflow/hf-cache`          | Whisper model downloads (via `HF_HOME` in systemd)         |
| `~/.local/state/linuxflow/linuxflow.pid`     | Daemon PID for stop helpers                                |




**`config.json` keys** (`linuxflow.py` schema):


| Key                     | Meaning                                                                                                                     |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `model`                 | `tiny`, `base`, `small`, `medium`, `large-v3-turbo` (default `small`)                                                       |
| `language`              | `auto`, `en`, `es`, `fr`, `de`, `hi` (default `en`) — these are **the built-in presets** surfaced in trays/menus            |
| `hotkey`                | Parsed combo string stored after validation (supports optional **custom** combos from tray or **`--hotkey`** where allowed) |
| `clipboard_enabled`     | Copy transcript to clipboard (default true)                                                                                 |
| `paste_enabled`         | Auto paste / type-after-copy pipe (forces clipboard on when true)                                                           |
| `sound_notifications`   | Play **`sounds/start.wav`** / **`sounds/stop.wav`** toggles                                                                 |
| `append_space`          | Append trailing space before copy/paste (default true)                                                                      |
| `release_tail_buffer_s` | Daemon-only extra seconds sampled after releasing the hotkey to reduce clipping (**0.1–1.5**, default **`0.55`**)           |
| `icon_theme`            | `auto`, `light`, or **`dark`** for tray PNG variants (`icons/icon-<variant>-<state>-*.png`)                                 |




The keys **`append_space`**, **`release_tail_buffer_s`**, and **`icon_theme`** are not editable via **`python linuxflow.py --config`** — set them manually in **`config.json`**.



Changing tray **model** invokes **`systemctl --user restart linuxflow.service`** via **`notify-send`** feedback (success vs failure).



## Command-line modes


| Mode                        | Command                        | Notes                                                                                                                                                                                                                                                                                                                                                                                                             |
| --------------------------- | ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Daemon / tray / hotkeys** | `python linuxflow.py --daemon` | Records through **`parecord`/`pw-record`**. Persisted daemon settings load from **`config.json`**. First-run CLI flags (**`--model`**, **`--language`**, **`--hotkey`**, **`--no-clipboard`**, **`--no-paste`**) merge into config before the tray starts saving again. **`--hotkey`** is limited to presets listed in **`--help`**. **`--device` is unsupported** — parser errors if paired with **`--daemon`**. |
| **Terminal dictation test** | `python linuxflow.py`          | **`--device N`** selects PyAudio mic index (**`--devices`** lists inputs). Clipboard/paste obey **`--no-clipboard`** / **`--no-paste`**. Clipboard insert uses default trailing-space behavior here (daemon honors **`append_space`** from config).                                                                                                                                                       |
| **TTY settings wizard**     | `python linuxflow.py --config` | Same subset as tray for model/language/hotkey/clipboard/paste/sound plus **start at login** (systemd user); optional immediate **`systemctl --user restart`**. Missing advanced JSON keys—edit **`config.json`** manually.                                                                                                                                                                                                                   |




Extra utilities:

```bash
python linuxflow.py --devices
python linuxflow.py --transcript 5    # last N lines from transcript_log.md to stdout
python linuxflow.py --open-transcript-log # xdg-opens transcript_log.md
```



### Full flag table


| Flag                             | Effect                                                                                                            |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `--daemon`                       | Tray + keyboard listener                                                                                          |
| `--model NAME`                   | ASR backbone profile (preset list in `--help`; default CLI default `small` before persistence merge at tray boot) |
| `--language LANG`                | `auto` / `en` / `es` / `fr` / `de` / `hi`                                                                         |
| `--hotkey COMBO`                 | Preset combos only (**Ctrl+Super+Z** default)—custom combos require tray dialog or **`config.json`**              |
| `--no-clipboard` / `--no-paste`  | Disable clipboard / downstream paste (**daemon**: writes false into **`config.json`**)                            |
| `--device N`                     | Mic index (**terminal-only**)                                                                                     |
| `--devices`                      | List PyAudio captures & exit                                                                                      |
| `--config`                       | Interactive menu & exit                                                                                           |
| `--asr-timeout`, `--asr-retries` | Worker timeout/backoff knobs                                                                                      |
| `--transcript N`                 | Print last *N* transcript bodies from the log to stdout (text only) and exit                                      |
| `--debug-transcript`             | **Daemon only:** print transcript snippets to stderr (may leak dictation into **`journalctl`**)                   |
| `--open-transcript-log`          | Open Markdown history externally                                                                                  |


Bundled WAV hotkey cues live in **`sounds/`**.



## Typical installation flow

Requirements: 

- microphone, 
- PulseAudio-compatible stack or PipeWire (**`pulseaudio-utils`** installs **`parecord`**; **`pipewire`** supplies **`pw-record`**), 
- **`python3`**, plus distro tooling for **`venv`** and **`pip`**



```bash
./install.sh
systemctl --user status linuxflow.service
journalctl --user -u linuxflow.service -f
```

```bash
systemctl --user restart linuxflow.service
```



Installed Python deps (`requirements.txt`):

```
faster-whisper>=1.0.0
PyAudio>=0.2.13
numpy>=1.24.0
evdev>=1.7.0
pystray>=0.19.0
Pillow>=10.0.0
```



Manual non-install dev runs reuse **`./venv`** or `./venv/bin/python linuxflow.py --daemon`.



### `start.sh`

The systemd unit invokes **`~/.../linuxflow/start.sh`** which resolves **`./venv/bin/python`** if present, prefers direct `/dev/input` readability, falls back **`sg input`**, otherwise aborts reminding you install/re-login.



### `stop.sh`

Stops the user systemd unit if possible, verifies **`linuxflow`** cmdline PID file side effects, terminates stray processes politely.



### Uninstall

```bash
./uninstall.sh
```

Removes service + desktop launcher + synced app tree (+ legacy **`udev`** file if lingering). Leaves **`input`** membership unless manually removed afterwards.



### User service sandbox

The generated unit enables **`ProtectSystem=full`**, **`NoNewPrivileges=yes`**, narrowed **`ReadWritePaths`**, plus kernel/personality hardening knobs—if you tighten further, preserve write access beneath **`~/.config/linuxflow`** and **`~/.local/state/linuxflow`**.



---

# Model guidance (informal)

Rough machine-class guidance (VRAM / RAM-heavy models need headroom—the first download can be sizable):


| Model                       | Notes                                                      |
| --------------------------- | ---------------------------------------------------------- |
| `tiny` / `base`             | Fastest, lowest quality                                    |
| `small`                     | Default balance                                            |
| `medium` / `large-v3-turbo` | Heavier, better accuracy—ensure CPU RAM + disk cache space |


---

# Architecture summary


| Concern         | Implementation                                                                                                                                                  |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Global hotkey   | **`evdev`** readers on keyboard **`InputDevice`** nodes                                                                                                           |
| Daemon audio    | External **`parecord`** (Pulse/PipeWire compat) or **`pw-record`**                                                                                              |
| Terminal audio  | **`PyAudio`** streaming                                                                                                                                         |
| ASR             | **`faster-whisper`** **`WhisperModel`** in a **`spawn`** multiprocessing worker (CPU **`int8`**), optional **`transcribe_tail_pad`**, VAD on recordings ≥ 1 s in backend |
| Clipboard       | **`wl-copy`** / **`xclip`**                                                                                                                                     |
| Paste           | Synthetic **Ctrl+V** via **`ydotool`**, X11 **`xdotool`**, textual **`ydotool type`** fallback                                                                  |
| Tray / menus    | **`pystray`** + Pillow **PNG icons** with filenames prefixed **`icon-*`**                                                                                       |
| Optional sounds | **`pw-play`** / **`paplay`** / **`aplay`**                                                                                                                      |
| Alerts          | **`notify-send`**                                                                                                                                               |
| Diagnostics     | Structured logging **`log_event`** to rotating file                                                                                                             |


---

# Troubleshooting shortcuts

**No keyboard readable / hotkey dead**: confirm **`input`** group (`id -Gn`), re-login, reinstall. **`PermissionError`** in logs ⇒ same.

**Tray missing on GNOME / KDE derivatives**: distro **`libayatana-appindicator`** (or analogous) packages; extension “AppIndicator & KStatusNotifier” on GNOME if needed.

**Recorder missing**: install **`pulseaudio-utils`** or ensure **`pw-record`** exists.

**Paste failure on Wayland**: start the **`ydotool`** daemon your distro ships (userspace input injection). Typical hint from the app: **`ydotoold`** running alongside **`ydotool`**.

**ALSA/GTK chatter**: suppressed by **`LINUXFLOW_SUPPRESS_ALSA_GTK_WARNINGS=1`** unless set **`0|false|no`** for raw logs.

# License

MIT