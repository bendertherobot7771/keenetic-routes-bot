#!/bin/sh

case "${1:-}" in
    start|stop|restart|status)
        /opt/bin/keenetic-routes-bot "$1"
        ;;
    check)
        /opt/bin/keenetic-routes-bot status
        ;;
    kill)
        /opt/bin/keenetic-routes-bot stop
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|check|kill}" >&2
        exit 2
        ;;
esac
