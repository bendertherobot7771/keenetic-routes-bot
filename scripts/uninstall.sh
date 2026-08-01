#!/bin/sh

set -eu

APP_DIR="/opt/apps/keenetic-routes-bot"
CONFIG_DIR="/opt/etc/keenetic-routes-bot"
INIT_SCRIPT="/opt/etc/init.d/S99keenetic-routes-bot"
CONTROL_SCRIPT="/opt/bin/keenetic-routes-bot"
PID_FILE="/opt/var/run/keenetic-routes-bot.pid"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run the uninstaller as root." >&2
    exit 1
fi
if [ -x "$CONTROL_SCRIPT" ]; then
    "$CONTROL_SCRIPT" stop || true
fi

rm -f "$INIT_SCRIPT" "$CONTROL_SCRIPT" "$PID_FILE"
rm -rf "$APP_DIR"

if [ "${1:-}" = "--purge" ]; then
    rm -rf "$CONFIG_DIR"
    echo "Application and configuration removed."
else
    echo "Application removed. Configuration preserved in $CONFIG_DIR."
    echo "Use --purge to remove it too."
fi
