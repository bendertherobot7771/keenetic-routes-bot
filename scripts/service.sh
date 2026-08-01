#!/bin/sh

APP_DIR="/opt/apps/keenetic-routes-bot"
CONFIG_FILE="/opt/etc/keenetic-routes-bot/config.env"
PID_FILE="/opt/var/run/keenetic-routes-bot.pid"
LOG_FILE="/opt/var/log/keenetic-routes-bot.log"
PYTHON="/opt/bin/python3"
DAEMONIZE="/opt/bin/daemonize"

is_running() {
    [ -f "$PID_FILE" ] || return 1
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

start_service() {
    if is_running; then
        echo "Keenetic Routes Bot is already running (PID $(cat "$PID_FILE"))."
        return 0
    fi
    if [ ! -x "$PYTHON" ]; then
        echo "Python not found: $PYTHON" >&2
        return 1
    fi
    if [ ! -x "$DAEMONIZE" ]; then
        echo "daemonize not found: $DAEMONIZE" >&2
        return 1
    fi
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "Configuration not found: $CONFIG_FILE" >&2
        return 1
    fi
    mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"
    touch "$LOG_FILE"
    cd "$APP_DIR" || return 1
    "$DAEMONIZE" \
        -c "$APP_DIR" \
        -p "$PID_FILE" \
        -o "$LOG_FILE" \
        -e "$LOG_FILE" \
        "$PYTHON" -m keenetic_routes_bot.main --config "$CONFIG_FILE"
    sleep 1
    if is_running; then
        echo "Keenetic Routes Bot started (PID $(cat "$PID_FILE"))."
        return 0
    fi
    echo "Keenetic Routes Bot failed to start. See $LOG_FILE." >&2
    return 1
}

stop_service() {
    if ! is_running; then
        rm -f "$PID_FILE"
        echo "Keenetic Routes Bot is not running."
        return 0
    fi
    pid="$(cat "$PID_FILE")"
    kill "$pid" 2>/dev/null || true
    counter=0
    while kill -0 "$pid" 2>/dev/null && [ "$counter" -lt 10 ]; do
        sleep 1
        counter=$((counter + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "Process did not stop in time; sending SIGKILL." >&2
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "Keenetic Routes Bot stopped."
}

status_service() {
    if is_running; then
        echo "Keenetic Routes Bot is running (PID $(cat "$PID_FILE"))."
        return 0
    fi
    echo "Keenetic Routes Bot is stopped."
    return 1
}

check_service() {
    cd "$APP_DIR" || return 1
    "$PYTHON" -m keenetic_routes_bot.main --config "$CONFIG_FILE" --check
}

show_help() {
    cat <<'EOF'
Usage: keenetic-routes-bot COMMAND

Commands:
  start       Start the bot
  stop        Stop the bot
  restart     Restart the bot
  status      Show process status
  check       Check configuration and Keenetic RCI
  logs        Follow the service log
  help        Show this help
EOF
}

case "${1:-help}" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        stop_service
        start_service
        ;;
    status)
        status_service
        ;;
    check)
        check_service
        ;;
    logs)
        touch "$LOG_FILE"
        tail -f "$LOG_FILE"
        ;;
    help|-h|--help)
        show_help
        ;;
    *)
        echo "Unknown command: $1" >&2
        show_help
        exit 2
        ;;
esac
