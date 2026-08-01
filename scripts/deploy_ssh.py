#!/usr/bin/env python3
"""Deploy the Entware archive over SSH without storing credentials on disk."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import shlex
import sys
from pathlib import Path

import paramiko


def run(client: paramiko.SSHClient, command: str, *, timeout: int = 300) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.close()
    code = stdout.channel.recv_exit_status()
    return code, stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")


def upload(client: paramiko.SSHClient, data: bytes, remote_path: str, mode: int | None = None) -> None:
    command = f"cat > {shlex.quote(remote_path)}"
    if mode is not None:
        command += f" && chmod {mode:o} {shlex.quote(remote_path)}"
    stdin, stdout, stderr = client.exec_command(command, timeout=300)
    stdin.channel.sendall(data)
    stdin.channel.shutdown_write()
    code = stdout.channel.recv_exit_status()
    error = stderr.read().decode(errors="replace")
    if code:
        raise RuntimeError(f"Upload failed for {remote_path}: {error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()

    password = os.environ.get("KRB_SSH_PASSWORD")
    bot_token = os.environ.get("KRB_BOT_TOKEN")
    allowed_users = os.environ.get("KRB_ALLOWED_USERS")
    if not password or not bot_token or not allowed_users:
        print("Missing KRB_SSH_PASSWORD, KRB_BOT_TOKEN, or KRB_ALLOWED_USERS", file=sys.stderr)
        return 2

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        args.host,
        port=args.port,
        username=args.user,
        password=password,
        timeout=15,
        auth_timeout=15,
        banner_timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    key = client.get_transport().get_remote_server_key()
    fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    print(f"Connected; host key {key.get_name()} SHA256:{fingerprint}")

    code, out, err = run(
        client,
        "id; uname -a; test -d /opt/etc/init.d && echo entware-init=ok; "
        "command -v opkg; df -h /opt 2>/dev/null || true",
    )
    print(out, end="")
    if code:
        print(err, file=sys.stderr, end="")
        return code

    remote_archive = "/opt/tmp/keenetic-routes-bot-entware.tar.gz"
    remote_root = "/opt/tmp/keenetic-routes-bot-0.1.0"
    upload(client, args.archive.read_bytes(), remote_archive)
    print(f"Uploaded {args.archive.name}")

    commands = [
        f"mkdir -p /opt/tmp && tar -xzf {shlex.quote(remote_archive)} -C /opt/tmp",
        "opkg update",
        "opkg install python3 ca-certificates daemonize",
        "mkdir -p /opt/apps/keenetic-routes-bot /opt/etc/keenetic-routes-bot /opt/var/log /opt/var/run /opt/bin",
        "/opt/bin/keenetic-routes-bot stop >/dev/null 2>&1 || true",
        "rm -rf /opt/apps/keenetic-routes-bot/keenetic_routes_bot",
        f"cp -R {remote_root}/keenetic_routes_bot /opt/apps/keenetic-routes-bot/keenetic_routes_bot",
        f"cp {remote_root}/pyproject.toml /opt/apps/keenetic-routes-bot/pyproject.toml",
        f"cp {remote_root}/README.md /opt/apps/keenetic-routes-bot/README.md",
        f"cp {remote_root}/LICENSE /opt/apps/keenetic-routes-bot/LICENSE",
        f"cp {remote_root}/scripts/service.sh /opt/bin/keenetic-routes-bot",
        f"cp {remote_root}/scripts/init.d.sh /opt/etc/init.d/S99keenetic-routes-bot",
        "chmod 755 /opt/bin/keenetic-routes-bot /opt/etc/init.d/S99keenetic-routes-bot",
    ]
    for command in commands:
        code, out, err = run(client, command)
        if out:
            print(out, end="")
        if code:
            print(f"Failed: {command}", file=sys.stderr)
            print(err, file=sys.stderr, end="")
            return code

    source_root = Path(__file__).resolve().parent.parent
    upload(client, (source_root / "scripts/service.sh").read_bytes(), "/opt/bin/keenetic-routes-bot", 0o755)
    upload(
        client,
        (source_root / "scripts/init.d.sh").read_bytes(),
        "/opt/etc/init.d/S99keenetic-routes-bot",
        0o755,
    )

    config = "\n".join(
        [
            f'BOT_TOKEN="{bot_token}"',
            f'ALLOWED_USERS="{allowed_users}"',
            'RCI_URL="http://127.0.0.1:79/rci"',
            'RCI_TOKEN=""',
            'RCI_TOKEN_HEADER="Authorization"',
            'RCI_TOKEN_PREFIX="Bearer "',
            'DEFAULT_INTERFACE="u1Host"',
            'PRIVATE_CHATS_ONLY="true"',
            'LOG_LEVEL="INFO"',
            'LOG_FILE="/opt/var/log/keenetic-routes-bot.log"',
            'POLL_TIMEOUT="25"',
            'REQUEST_TIMEOUT="15"',
            'MAX_GROUP_ENTRIES="300"',
            "",
        ]
    )
    upload(client, config.encode(), "/opt/etc/keenetic-routes-bot/config.env", 0o600)
    print("Configuration installed with mode 600")

    for command in [
        "/opt/bin/keenetic-routes-bot check",
        "/opt/bin/keenetic-routes-bot start",
        "/opt/bin/keenetic-routes-bot status",
        "sleep 2; tail -n 30 /opt/var/log/keenetic-routes-bot.log",
    ]:
        code, out, err = run(client, command)
        if out:
            print(out, end="")
        if err:
            print(err, file=sys.stderr, end="")
        if code:
            return code

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
