"""Interactive LabVault appliance SSH REPL (no OS shell)."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from asgiref.sync import sync_to_async
from django.db import close_old_connections

from connect.labvault_cli import commands as _commands  # noqa: F401
from connect.labvault_cli.runner import CliContext, invoke, prepare_confirm


BANNER = "LabVault appliance CLI. Type `help`. Type `exit` to disconnect.\n"
PROMPT = "labvault> "


class ApplianceSSHSession:
    """Minimal line-oriented REPL bound to the shared CLI runner."""

    def __init__(self, process, user, remote_addr: str):
        self.process = process
        self.user = user
        self.remote_addr = remote_addr or ""
        self.session_id = uuid.uuid4().hex

    def _ctx(self) -> CliContext:
        return CliContext(
            user=self.user,
            source="ssh",
            remote_addr=self.remote_addr,
            session_id=self.session_id,
        )

    async def _prepare(self, line: str):
        def _run():
            close_old_connections()
            try:
                return prepare_confirm(self._ctx(), line)
            finally:
                close_old_connections()

        return await sync_to_async(_run, thread_sensitive=False)()

    async def _invoke(self, line: str, confirmation_nonce: str | None = None):
        def _run():
            close_old_connections()
            try:
                return invoke(
                    self._ctx(),
                    line=line,
                    confirmation_nonce=confirmation_nonce,
                )
            finally:
                close_old_connections()

        return await sync_to_async(_run, thread_sensitive=False)()

    async def _readline(self, prompt: str) -> str | None:
        self.process.stdout.write(prompt)
        line = await self.process.stdin.readline()
        if not line:
            return None
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        return line.rstrip("\r\n")

    async def run(self) -> None:
        self.process.stdout.write(BANNER)
        while True:
            try:
                line = await self._readline(PROMPT)
            except Exception:  # noqa: BLE001
                break
            if line is None:
                break
            raw = line.strip()
            if not raw:
                continue
            low = raw.lower()
            if low in ("exit", "quit", "logout"):
                self.process.stdout.write("goodbye\n")
                break
            try:
                prep = await self._prepare(raw)
            except Exception:  # noqa: BLE001
                self.process.stdout.write("error: command failed\n")
                continue

            nonce = None
            if prep.ok and prep.payload.get("confirmation_required"):
                detail = (
                    f"Confirm {prep.payload.get('action') or prep.payload.get('command')}"
                    f" target={prep.payload.get('target') or '-'}"
                    f" reason={prep.payload.get('reason') or '-'}"
                    " [y/N]: "
                )
                ans = await self._readline(detail)
                if not ans or ans.strip().lower() not in ("y", "yes"):
                    self.process.stdout.write("cancelled\n")
                    continue
                nonce = prep.payload.get("confirmation_nonce")
            elif not prep.ok and prep.payload.get("error") not in (
                "confirmation_not_required",
                "confirmation_not_needed",
            ):
                # Non-mutating commands return confirmation_not_required — fall through to invoke.
                if prep.status >= 400 and prep.payload.get("error") not in (
                    "confirmation_not_required",
                    "confirmation_not_needed",
                ):
                    self._write_result(prep.payload)
                    continue

            try:
                result = await self._invoke(raw, confirmation_nonce=nonce)
            except Exception:  # noqa: BLE001
                self.process.stdout.write("error: command failed\n")
                continue
            self._write_result(result.payload if hasattr(result, "payload") else result)

        self.process.exit(0)

    def _write_result(self, payload: Any) -> None:
        import json

        if not isinstance(payload, dict):
            payload = {"result": payload}
        # Keep output bounded and redacted (runner already redacts).
        text = json.dumps(payload, indent=2, default=str)
        if len(text) > 20000:
            text = text[:20000] + "\n...truncated...\n"
        self.process.stdout.write(text + "\n")


async def handle_client(process) -> None:
    """AsyncSSH process factory callback."""
    # Connection/auth already validated; user attached on connection object.
    conn = process.channel.get_extra_info("connection")
    user = getattr(conn, "labvault_user", None)
    peer = process.get_extra_info("peername")
    remote = ""
    if peer and isinstance(peer, (list, tuple)) and peer:
        remote = str(peer[0])
    if user is None:
        process.stdout.write("authentication required\n")
        process.exit(1)
        return
    session = ApplianceSSHSession(process, user=user, remote_addr=remote)
    await session.run()
