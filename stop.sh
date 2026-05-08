#!/bin/bash
# Stop LinuxFlow - works for both systemd and manual runs
PID_FILE="$HOME/.local/state/linuxflow/linuxflow.pid"

if systemctl --user is-active linuxflow.service &>/dev/null; then
    systemctl --user stop linuxflow.service
    echo "LinuxFlow service stopped."
else
    if [ -f "$PID_FILE" ]; then
        pid="$(cat "$PID_FILE" 2>/dev/null)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            sleep 0.4
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null
            fi
            rm -f "$PID_FILE"
            echo "LinuxFlow stopped."
        else
            rm -f "$PID_FILE"
            echo "LinuxFlow not running."
        fi
    else
        echo "LinuxFlow not running."
    fi
fi
