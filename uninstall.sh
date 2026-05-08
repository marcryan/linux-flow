#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${HOME}/.local/share/linuxflow"
SERVICE_PATH="${HOME}/.config/systemd/user/linuxflow.service"
DESKTOP_PATH="${HOME}/.local/share/applications/linuxflow.desktop"
UDEV_RULE_PATH="/etc/udev/rules.d/70-linuxflow-input.rules"

run_sudo() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "sudo is required to remove udev rule." >&2
    exit 1
  fi
}

echo "Uninstalling LinuxFlow..."

if [[ -f "$SERVICE_PATH" ]] || systemctl --user status linuxflow.service >/dev/null 2>&1; then
  systemctl --user disable --now linuxflow.service || true
  systemctl --user daemon-reload
fi

rm -f "$SERVICE_PATH"
rm -f "$DESKTOP_PATH"
rm -rf "$APP_DIR"

if [[ -f "$UDEV_RULE_PATH" ]]; then
  run_sudo rm -f "$UDEV_RULE_PATH"
  run_sudo udevadm control --reload-rules
  run_sudo udevadm trigger --subsystem-match=input
fi

echo "LinuxFlow uninstalled."
