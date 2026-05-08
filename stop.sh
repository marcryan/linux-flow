#!/usr/bin/env bash
set -euo pipefail

PID_FILE="$HOME/.local/state/linuxflow/linuxflow.pid"

is_linuxflow_pid() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q "linuxflow"
}

if systemctl --user stop linuxflow.service >/dev/null 2>&1; then
  echo "LinuxFlow service stop requested."
fi

if systemctl --user is-active linuxflow.service >/dev/null 2>&1; then
  echo "LinuxFlow service is still active after stop request."
fi

if [[ -f "$PID_FILE" ]]; then
  pid="$(<"$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    if is_linuxflow_pid "$pid"; then
      kill "$pid" 2>/dev/null || true
      sleep 0.4
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
      fi
      echo "LinuxFlow stopped."
    else
      echo "Refusing to signal PID $pid: process does not look like linuxflow."
    fi
  else
    echo "LinuxFlow not running."
  fi
  rm -f "$PID_FILE"
else
  echo "LinuxFlow not running."
fi
