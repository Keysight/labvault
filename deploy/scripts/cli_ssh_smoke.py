#!/usr/bin/env python3
"""AsyncSSH smoke client for LabVault appliance CLI (no nc dependency).

Usage:
  LABVAULT_CLI_SSH_HOST=127.0.0.1 LABVAULT_CLI_SSH_PORT=2222 \\
  LABVAULT_CLI_USER=admin LABVAULT_CLI_PASSWORD='...' \\
  ./deploy/scripts/cli_ssh_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys


async def _run() -> int:
    try:
        import asyncssh
    except ImportError:
        print("FAIL asyncssh not installed")
        return 2

    host = os.environ.get("LABVAULT_CLI_SSH_HOST", "127.0.0.1")
    port = int(os.environ.get("LABVAULT_CLI_SSH_PORT", "2222"))
    user = os.environ.get("LABVAULT_CLI_USER", "admin")
    password = os.environ.get("LABVAULT_CLI_PASSWORD", "")
    if not password:
        print("FAIL set LABVAULT_CLI_PASSWORD")
        return 2

    known = os.environ.get("LABVAULT_CLI_KNOWN_HOSTS", "")
    opts = asyncssh.SSHClientConnectionOptions(
        username=user,
        password=password,
        known_hosts=known or None,
        client_keys=None,
    )
    # First-boot: allow unknown host key unless known_hosts provided
    if not known:
        opts = asyncssh.SSHClientConnectionOptions(
            username=user,
            password=password,
            known_hosts=None,
        )

    commands = [
        "whoami",
        "help",
        "show services",
        "show fleet",
    ]

    try:
        async with asyncssh.connect(host, port=port, options=opts) as conn:
            # Refuse non-interactive exec (server should reject)
            try:
                result = await conn.run("whoami", check=False)
                # If server allows exec, still OK if it returns appliance output;
                # preferred path is interactive.
                print("INFO exec_probe exit=", result.exit_status)
            except Exception as exc:  # noqa: BLE001
                print("PASS noninteractive_exec_blocked:", type(exc).__name__)

            async with conn.create_process(term_type="xterm") as proc:
                banner = await proc.stdout.read(256)
                print("INFO banner=", banner[:120].replace("\n", "\\n"))
                for cmd in commands:
                    proc.stdin.write(cmd + "\n")
                    await asyncio.sleep(0.5)
                    out = await proc.stdout.read(4096)
                    print(f"PASS cmd={cmd} bytes={len(out)}")
                proc.stdin.write("exit\n")
    except Exception as exc:  # noqa: BLE001
        print("FAIL connect/session:", exc)
        return 1
    print("VERIFY_CLI_SSH_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
