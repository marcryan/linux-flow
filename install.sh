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
DESKTOP_ICON_PATH="${APP_DIR}/icons/dark-idle-1024.png"

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
    echo "sudo is required to install system dependencies and udev rules." >&2
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
  "${VENV_DIR}/bin/pip" install --upgrade pip
  "${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt"
}

install_udev_rule() {
  run_sudo tee "$UDEV_RULE_PATH" >/dev/null <<'EOF'
KERNEL=="event*", SUBSYSTEM=="input", ENV{ID_INPUT_KEYBOARD}=="1", TAG+="uaccess"
EOF
  run_sudo udevadm control --reload-rules
  run_sudo udevadm trigger --subsystem-match=input
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

[Install]
WantedBy=graphical-session.target
EOF

  if systemctl --user daemon-reload >/dev/null 2>&1; then
    systemctl --user enable --now linuxflow.service
  else
    warn "Could not talk to systemd user manager in this session."
    warn "Run manually after login: systemctl --user daemon-reload && systemctl --user enable --now linuxflow.service"
  fi
}

install_desktop_entry() {
  local desktop_icon="$DESKTOP_ICON_PATH"
  local color_scheme gtk_theme kde_scheme

  if command -v gsettings >/dev/null 2>&1; then
    color_scheme="$(gsettings get org.gnome.desktop.interface color-scheme 2>/dev/null || true)"
    gtk_theme="$(gsettings get org.gnome.desktop.interface gtk-theme 2>/dev/null || true)"
    if [[ "$color_scheme" == *"prefer-dark"* || "$gtk_theme" == *"dark"* || "$gtk_theme" == *"Dark"* ]]; then
      desktop_icon="${APP_DIR}/icons/light-idle-1024.png"
    fi
  fi

  if [[ -f "${HOME}/.config/kdeglobals" ]]; then
    kde_scheme="$(awk -F= '/^\s*ColorScheme\s*=/{print $2; exit}' "${HOME}/.config/kdeglobals" 2>/dev/null || true)"
    if [[ "$kde_scheme" == *"Dark"* || "$kde_scheme" == *"dark"* ]]; then
      desktop_icon="${APP_DIR}/icons/light-idle-1024.png"
    elif [[ -n "$kde_scheme" ]]; then
      desktop_icon="${APP_DIR}/icons/dark-idle-1024.png"
    fi
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
Categories=Utility;AudioVideo;
StartupNotify=false
EOF
}

main() {
  echo "Installing LinuxFlow..."
  install_system_dependencies
  sync_app_files
  setup_python_env
  install_udev_rule
  install_user_service
  install_desktop_entry

  echo
  echo "LinuxFlow is installed."
  echo "Service status: systemctl --user status linuxflow.service"
  echo "Live logs:       journalctl --user -u linuxflow -f"
  echo
  echo "If hotkeys do not work immediately, log out and log back in once."
}

main "$@"
