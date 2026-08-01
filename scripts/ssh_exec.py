#!/usr/bin/env python3
"""Run a diagnostic command over SSH, taking the password from the environment."""

from __future__ import annotations

import argparse
import os
import sys

import paramiko


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("command", nargs="?")
    args = parser.parse_args()
    password = os.environ.get("KRB_SSH_PASSWORD")
    command = args.command or os.environ.get("KRB_SSH_COMMAND")
    if not password:
        print("Missing KRB_SSH_PASSWORD", file=sys.stderr)
        return 2
    if not command:
        print("Missing command or KRB_SSH_COMMAND", file=sys.stderr)
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
        look_for_keys=False,
        allow_agent=False,
    )
    _, stdout, stderr = client.exec_command(command, timeout=300)
    code = stdout.channel.recv_exit_status()
    sys.stdout.write(stdout.read().decode(errors="replace"))
    sys.stderr.write(stderr.read().decode(errors="replace"))
    client.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
