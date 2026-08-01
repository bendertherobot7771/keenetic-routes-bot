#!/bin/sh

set -eu

APP_DIR="/opt/apps/keenetic-routes-bot"
CONFIG_DIR="/opt/etc/keenetic-routes-bot"
CONFIG_FILE="$CONFIG_DIR/config.env"
INIT_SCRIPT="/opt/etc/init.d/S99keenetic-routes-bot"
CONTROL_SCRIPT="/opt/bin/keenetic-routes-bot"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SOURCE_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run the installer as root." >&2
    exit 1
fi

if [ ! -d /opt/etc/init.d ] || ! command -v opkg >/dev/null 2>&1; then
    echo "Entware is not installed or /opt is not mounted." >&2
    exit 1
fi

if [ ! -d "$SOURCE_DIR/keenetic_routes_bot" ]; then
    echo "Run scripts/install.sh from an unpacked project directory." >&2
    exit 1
fi

echo "[1/5] Installing Entware dependencies..."
opkg update
opkg install python3 ca-certificates daemonize

echo "[2/5] Installing application files..."
if [ -x "$CONTROL_SCRIPT" ]; then
    "$CONTROL_SCRIPT" stop >/dev/null 2>&1 || true
fi
mkdir -p "$APP_DIR" "$CONFIG_DIR" /opt/var/log /opt/var/run /opt/bin
rm -rf "$APP_DIR/keenetic_routes_bot"
cp -R "$SOURCE_DIR/keenetic_routes_bot" "$APP_DIR/keenetic_routes_bot"
cp "$SOURCE_DIR/pyproject.toml" "$APP_DIR/pyproject.toml"
if [ -f "$SOURCE_DIR/README.md" ]; then
    cp "$SOURCE_DIR/README.md" "$APP_DIR/README.md"
fi
if [ -f "$SOURCE_DIR/LICENSE" ]; then
    cp "$SOURCE_DIR/LICENSE" "$APP_DIR/LICENSE"
fi

echo "[3/5] Installing service scripts..."
cp "$SOURCE_DIR/scripts/service.sh" "$CONTROL_SCRIPT"
cp "$SOURCE_DIR/scripts/init.d.sh" "$INIT_SCRIPT"
chmod 755 "$CONTROL_SCRIPT" "$INIT_SCRIPT"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "[4/5] Creating configuration..."
    printf "Telegram bot token: "
    trap 'stty echo 2>/dev/null || true' 0 HUP INT TERM
    stty -echo
    IFS= read -r BOT_TOKEN_INPUT
    stty echo
    trap - 0 HUP INT TERM
    printf "\n"
    printf "Allowed Telegram user IDs (comma-separated): "
    IFS= read -r ALLOWED_USERS_INPUT
    printf "Default Keenetic interface ID [u1Host]: "
    IFS= read -r DEFAULT_INTERFACE_INPUT
    DEFAULT_INTERFACE_INPUT="${DEFAULT_INTERFACE_INPUT:-u1Host}"
    umask 077
    {
        printf 'BOT_TOKEN="%s"\n' "$BOT_TOKEN_INPUT"
        printf 'ALLOWED_USERS="%s"\n' "$ALLOWED_USERS_INPUT"
        printf 'RCI_URL="http://127.0.0.1:79/rci"\n'
        printf 'RCI_TOKEN=""\n'
        printf 'RCI_TOKEN_HEADER="Authorization"\n'
        printf 'RCI_TOKEN_PREFIX="Bearer "\n'
        printf 'DEFAULT_INTERFACE="%s"\n' "$DEFAULT_INTERFACE_INPUT"
        printf 'PRIVATE_CHATS_ONLY="true"\n'
        printf 'LOG_LEVEL="INFO"\n'
        printf 'LOG_FILE="/opt/var/log/keenetic-routes-bot.log"\n'
        printf 'POLL_TIMEOUT="25"\n'
        printf 'REQUEST_TIMEOUT="15"\n'
        printf 'MAX_GROUP_ENTRIES="300"\n'
    } > "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
else
    echo "[4/5] Keeping existing configuration: $CONFIG_FILE"
fi

echo "[5/5] Checking Keenetic RCI..."
if ! "$CONTROL_SCRIPT" check; then
    echo "The application is installed, but the RCI check failed." >&2
    echo "Fix $CONFIG_FILE and run: $CONTROL_SCRIPT start" >&2
    exit 1
fi

"$CONTROL_SCRIPT" start
echo
echo "Installation complete."
echo "Control: $CONTROL_SCRIPT {start|stop|restart|status|check|logs}"
echo "Config:  $CONFIG_FILE"
