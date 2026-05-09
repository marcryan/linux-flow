#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${HOME}/.local/share/linuxflow"
VENV_DIR="${APP_DIR}/venv"
SERVICE_DIR="${HOME}/.config/systemd/user"
SERVICE_PATH="${SERVICE_DIR}/linuxflow.service"
DESKTOP_DIR="${HOME}/.local/share/applications"
DESKTOP_PATH="${DESKTOP_DIR}/linuxflow.desktop"
RUNNER_PATH="${APP_DIR}/start.sh"
UDEV_RULE_PATH="/etc/udev/rules.d/70-linuxflow-input.rules"
DESKTOP_ICON_PATH="${APP_DIR}/icons/linuxflowicon.svg"

warn() {
  echo "WARN: $*" >&2
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

run_sudo() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "sudo is required to install system dependencies, manage input group membership, and remove legacy udev rules." >&2
    exit 1
  fi
}

detect_package_manager() {
  if command -v pacman >/dev/null 2>&1; then
    echo "pacman"
    return
  fi
  if command -v apt-get >/dev/null 2>&1; then
    echo "apt"
    return
  fi
  if command -v dnf >/dev/null 2>&1; then
    echo "dnf"
    return
  fi
  echo ""
}

has_package_pacman() {
  pacman -Si "$1" >/dev/null 2>&1
}

has_package_apt() {
  apt-cache show "$1" >/dev/null 2>&1
}

has_package_dnf() {
  dnf info "$1" >/dev/null 2>&1
}

install_if_available() {
  local pm="$1"
  shift
  local pkg
  local to_install=()
  for pkg in "$@"; do
    case "$pm" in
      pacman)
        if has_package_pacman "$pkg"; then
          to_install+=("$pkg")
        else
          warn "Skipping unavailable package: $pkg"
        fi
        ;;
      apt)
        if has_package_apt "$pkg"; then
          to_install+=("$pkg")
        else
          warn "Skipping unavailable package: $pkg"
        fi
        ;;
      dnf)
        if has_package_dnf "$pkg"; then
          to_install+=("$pkg")
        else
          warn "Skipping unavailable package: $pkg"
        fi
        ;;
    esac
  done

  if [[ "${#to_install[@]}" -eq 0 ]]; then
    return
  fi

  case "$pm" in
    pacman) run_sudo pacman -S --needed --noconfirm "${to_install[@]}" ;;
    apt) run_sudo apt-get install -y "${to_install[@]}" ;;
    dnf) run_sudo dnf install -y "${to_install[@]}" ;;
  esac
}

install_system_dependencies() {
  local pm
  pm="$(detect_package_manager)"
  case "$pm" in
    pacman)
      install_if_available "$pm" \
        python python-pip python-virtualenv portaudio ydotool wl-clipboard \
        libayatana-appindicator libappindicator-gtk3 libnotify python-gobject
      if ! pacman -Qq libayatana-appindicator >/dev/null 2>&1 && ! pacman -Qq libappindicator-gtk3 >/dev/null 2>&1; then
        warn "No AppIndicator package found (libayatana-appindicator/libappindicator-gtk3). Tray icon/menu may not work on some desktops."
      fi
      # GUI prompt for custom hotkey: prefer kdialog on KDE, zenity elsewhere.
      install_if_available "$pm" kdialog zenity

      # Never install pulseaudio when pipewire-pulse is present (they conflict).
      if pacman -Qq pipewire-pulse >/dev/null 2>&1; then
        echo "Detected pipewire-pulse; skipping pulseaudio install to avoid conflicts."
      elif pacman -Qq pulseaudio >/dev/null 2>&1; then
        echo "Detected pulseaudio."
      else
        # Optional: whichever exists in repos first.
        install_if_available "$pm" pipewire-pulse pulseaudio
      fi
      ;;
    apt)
      run_sudo apt-get update
      install_if_available "$pm" \
        python3 python3-pip python3-venv portaudio19-dev ydotool wl-clipboard \
        libayatana-appindicator3-1 pulseaudio-utils libnotify-bin python3-gi
      install_if_available "$pm" kdialog zenity
      ;;
    dnf)
      install_if_available "$pm" \
        python3 python3-pip python3-virtualenv portaudio-devel ydotool wl-clipboard \
        libayatana-appindicator-gtk3 pulseaudio-utils libnotify python3-gobject
      install_if_available "$pm" kdialog zenity
      ;;
    *)
      echo "Unsupported distro package manager. Install dependencies manually first." >&2
      exit 1
      ;;
  esac
}

sync_app_files() {
  mkdir -p "$APP_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude ".git/" \
      --exclude "venv/" \
      --exclude "__pycache__/" \
      --exclude ".pytest_cache/" \
      "$ROOT_DIR/" "$APP_DIR/"
  else
    rm -rf "${APP_DIR:?}/"*
    cp -a "$ROOT_DIR/." "$APP_DIR/"
    rm -rf "${APP_DIR}/.git" "${APP_DIR}/venv" "${APP_DIR}/__pycache__" "${APP_DIR}/.pytest_cache"
  fi
}

setup_python_env() {
  need_cmd python3
  if [[ -f "${VENV_DIR}/pyvenv.cfg" ]]; then
    if ! grep -Eq '^include-system-site-packages = true$' "${VENV_DIR}/pyvenv.cfg"; then
      rm -rf "$VENV_DIR"
    fi
  fi
  python3 -m venv --system-site-packages "$VENV_DIR"
  # -q keeps IDE terminals usable; set LINUXFLOW_PIP_VERBOSE=1 for full pip output
  local pip_flags=()
  [[ -z "${LINUXFLOW_PIP_VERBOSE:-}" ]] && pip_flags+=(-q)
  "${VENV_DIR}/bin/pip" install "${pip_flags[@]}" --upgrade pip
  "${VENV_DIR}/bin/pip" install "${pip_flags[@]}" -r "${APP_DIR}/requirements.txt"
}

cleanup_legacy_udev_rule() {
  if [[ -f "$UDEV_RULE_PATH" ]]; then
    echo "Removing legacy LinuxFlow udev rule (keyboard access now uses the input group)."
    run_sudo rm -f "$UDEV_RULE_PATH"
    echo "Reloading udev (this can take ~30s on some machines)…"
    run_sudo udevadm control --reload-rules
    run_sudo udevadm trigger --subsystem-match=input
  fi
}

ensure_input_group() {
  if ! getent group input >/dev/null 2>&1; then
    echo "ERROR: system group 'input' not found. Cannot configure keyboard device access." >&2
    exit 1
  fi
  if id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
    INPUT_GROUP_JUST_ADDED=0
    return 0
  fi
  run_sudo usermod -aG input "$USER"
  echo "Added ${USER} to 'input' group. Log out and back in for hotkeys to work without the sg input fallback."
  INPUT_GROUP_JUST_ADDED=1
}

install_user_service() {
  mkdir -p "$SERVICE_DIR"
  cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=LinuxFlow - Offline Voice Dictation
After=graphical-session.target pipewire.service

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${RUNNER_PATH}
Restart=on-failure
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=HF_HOME=%h/.local/state/linuxflow/hf-cache
NoNewPrivileges=yes
ProtectSystem=full
ProtectKernelTunables=yes
ProtectKernelModules=yes
RestrictSUIDSGID=yes
LockPersonality=yes
ReadWritePaths=%h/.config/linuxflow %h/.local/state/linuxflow

[Install]
WantedBy=graphical-session.target
EOF

  if ! systemctl --user daemon-reload >/dev/null 2>&1; then
    warn "Could not talk to systemd user manager in this session."
    warn "Run manually after login: systemctl --user daemon-reload && systemctl --user enable --now linuxflow.service"
    return 1
  fi
}

# Respect LINUXFLOW_AUTOSTART=1|yes / 0|no non-interactively; prompt on a tty by default (Y=yes).
# Prefer stdin when it is a TTY (Cursor/VS Code); only use /dev/tty when stdin is not interactive
# (e.g. curl ... | bash) — reading /dev/tty alone can hang indefinitely in some IDE terminals.
prompt_linuxflow_autostart_login() {
  local want_auto=1

  if [[ -n "${LINUXFLOW_AUTOSTART:-}" ]]; then
    case "${LINUXFLOW_AUTOSTART,,}" in
      0|false|no|n|off) want_auto=0 ;;
      *)
        want_auto=1
        ;;
    esac
  elif [[ -t 0 ]]; then
    local reply
    # shellcheck disable=SC2162
    read -r -p "Start LinuxFlow automatically when you log in? [Y/n] " reply || true
    case "${reply,,}" in
      n | no | false | 0 | off)
        want_auto=0
        ;;
      *)
        ;;
    esac
  elif [[ -c /dev/tty ]]; then
    local reply
    # shellcheck disable=SC2162
    if read -r -p "Start LinuxFlow automatically when you log in? [Y/n] " reply < /dev/tty 2>/dev/null; then
      case "${reply,,}" in
        n | no | false | 0 | off)
          want_auto=0
          ;;
        *)
          ;;
      esac
    fi
  fi

  if [[ "$want_auto" -eq 1 ]]; then
    if systemctl --user enable --now linuxflow.service >/dev/null 2>&1; then
      echo "Autostart enabled: LinuxFlow joins your graphical login session via systemd."
    else
      warn "Could not enable linuxflow.service. After next login:"
      warn "  systemctl --user enable --now linuxflow.service"
    fi
  else
    systemctl --user disable linuxflow.service >/dev/null 2>&1 || true
    if systemctl --user start linuxflow.service >/dev/null 2>&1; then
      echo "Autostart skipped; LinuxFlow started once for the current graphical session."
    else
      warn "Autostart skipped and could not start now. Later: systemctl --user start linuxflow.service"
    fi
  fi
}

install_desktop_entry() {
  # Application menu launcher (~/.local/share/applications/linuxflow.desktop).
  # Use branded linuxflowicon.svg; PNG tray rasters stay separate (icon-*/*.pystray).
  local desktop_icon="$DESKTOP_ICON_PATH"

  if [[ ! -f "${desktop_icon}" ]]; then
    warn "Missing ${desktop_icon}; falling back to raster idle icon."
    desktop_icon="${APP_DIR}/icons/icon-dark-idle-1024.png"
  fi
  if [[ ! -f "${desktop_icon}" ]]; then
    warn "No launcher icon found under ${APP_DIR}/icons/ (expected linuxflowicon.svg or icon-dark-idle-1024.png)."
  fi

  mkdir -p "$DESKTOP_DIR"
  cat > "$DESKTOP_PATH" <<EOF
[Desktop Entry]
Name=LinuxFlow
Comment=Offline voice dictation for Linux
Type=Application
Exec=systemctl --user restart linuxflow.service
Icon=${desktop_icon}
Terminal=false
Categories=AudioVideo;
StartupNotify=false
EOF

  # Help application menus refresh (KDE Kickoff/GNOME grid, etc.)
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${DESKTOP_DIR}" >/dev/null 2>&1 || true
  fi
}

install_cli_launcher() {
  local src="${APP_DIR}/linuxflow"
  local dest="${HOME}/.local/bin/linuxflow"
  if [[ ! -f "$src" ]]; then
    warn "CLI wrapper missing at $src (skipped ~/.local/bin/linuxflow)."
    return
  fi
  chmod +x "$src"
  mkdir -p "${HOME}/.local/bin"
  if PATH="/usr/bin:/bin:${PATH}" command -v install >/dev/null 2>&1; then
    PATH="/usr/bin:/bin:${PATH}" install -m 0755 "$src" "$dest"
  else
    cp -f "$src" "$dest"
    chmod 0755 "$dest"
  fi
}

# Fish often omits ~/.local/bin unless the user configured it — make `linuxflow` discoverable.
install_fish_local_bin_path() {
  if ! command -v fish >/dev/null 2>&1; then
    return 0
  fi
  local conf_dir="${HOME}/.config/fish/conf.d"
  local dropin="${conf_dir}/linuxflow-path.fish"
  mkdir -p "$conf_dir"
  cat >"${dropin}" <<'EOS'
### Added by LinuxFlow install.sh (~/.local/bin: linuxflow CLI, pip --user tools, …)
fish_add_path $HOME/.local/bin
EOS
  echo "Fish: ~/.config/fish/conf.d/linuxflow-path.fish — open a new terminal or run  exec fish  then try:  linuxflow"
}

main() {
  INPUT_GROUP_JUST_ADDED=0
  echo "Installing LinuxFlow..."
  install_system_dependencies
  sync_app_files
  setup_python_env
  cleanup_legacy_udev_rule
  ensure_input_group
  install_user_service
  prompt_linuxflow_autostart_login
  install_desktop_entry
  install_cli_launcher
  install_fish_local_bin_path

  echo
  echo "LinuxFlow is installed."
  echo "Service status: systemctl --user status linuxflow.service"
  echo "Live logs:       journalctl --user -u linuxflow -f"
  echo
  echo "Terminal CLI:  ~/.local/bin/linuxflow   (venv — any directory)."
  echo "Do not run:      python linuxflow.py     inside the repo copy without that repo's venv; use  linuxflow  or:"
  echo "                 ${HOME}/.local/share/linuxflow/venv/bin/python ${HOME}/.local/share/linuxflow/linuxflow.py"
  echo "Bash/zsh PATH: export PATH=\"\${HOME}/.local/bin:\${PATH}\"   if  command -v linuxflow  fails."
  echo
  if [[ "${INPUT_GROUP_JUST_ADDED}" -eq 1 ]]; then
    echo "If hotkeys do not work immediately, log out and log back in once."
  fi
}

main "$@"
