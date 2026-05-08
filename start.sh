#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="./venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

can_read_input_devices() {
  local event
  for event in /dev/input/event*; do
    [[ -e "$event" ]] || continue
    if [[ -r "$event" ]]; then
      return 0
    fi
  done
  return 1
}

if can_read_input_devices; then
  exec "$PYTHON_BIN" linuxflow.py --daemon
fi

if command -v sg >/dev/null 2>&1 && id -nG "$USER" | tr ' ' '\n' | grep -qx "input"; then
  exec sg input -c "$PYTHON_BIN linuxflow.py --daemon"
fi

echo "LinuxFlow cannot read /dev/input devices."
echo "Run install.sh (preferred) or add your user to the input group and re-login."
exit 1
