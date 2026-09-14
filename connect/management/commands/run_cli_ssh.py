"""Management command: LabVault appliance SSH CLI (AsyncSSH)."""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the LabVault appliance SSH CLI (no OS shell)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--host",
            default=os.environ.get("LABVAULT_CLI_SSH_BIND", "0.0.0.0"),
        )
        parser.add_argument(
            "--port",
            type=int,
            default=int(os.environ.get("LABVAULT_CLI_SSH_PORT", "2222")),
        )

    def handle(self, *args, **options):
        asyncio.run(self._serve(options["host"], options["port"]))

    async def _serve(self, host: str, port: int) -> None:
        try:
            import asyncssh
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("asyncssh is required for the appliance SSH CLI") from exc

        from connect.labvault_cli.ssh_auth import authenticate_staff
        from connect.labvault_cli.ssh_repl import handle_client

        key_path = Path(
            os.environ.get(
                "LABVAULT_CLI_SSH_HOST_KEY",
                "/var/lib/labvault/cli-ssh/ssh_host_ed25519_key",
            )
        )
        key_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(key_path.parent, 0o700)
        if not key_path.exists():
            key = asyncssh.generate_private_key("ssh-ed25519")
            tmp = key_path.with_suffix(key_path.suffix + ".tmp")
            key.write_private_key(str(tmp))
            os.chmod(tmp, 0o600)
            tmp.replace(key_path)
            self.stdout.write(f"generated host key {key_path}")
        else:
            os.chmod(key_path, 0o600)

        host_key = asyncssh.read_private_key(str(key_path))

        idle = int(os.environ.get("LABVAULT_CLI_SSH_IDLE_TIMEOUT", "600"))
        auth_timeout = int(os.environ.get("LABVAULT_CLI_SSH_AUTH_TIMEOUT", "60"))
        max_sessions = int(os.environ.get("LABVAULT_CLI_SSH_MAX_SESSIONS", "20"))
        active_sessions = {"n": 0}

        class _Server(asyncssh.SSHServer):
            def connection_made(self, conn):
                self._conn = conn
                peer = conn.get_extra_info("peername")
                self._remote = str(peer[0]) if peer else ""

            def connection_lost(self, exc):
                # Best-effort session accounting for concurrent limit.
                if getattr(self, "_counted", False):
                    active_sessions["n"] = max(0, active_sessions["n"] - 1)
                    self._counted = False

            def password_auth_supported(self):
                return True

            def begin_auth(self, username):
                return True

            async def validate_password(self, username, password):
                # Django ORM cannot run on the AsyncSSH event loop. Use a worker
                # thread (thread_sensitive=False) so we do not deadlock waiting
                # for the main loop that is blocked on this coroutine.
                from asgiref.sync import sync_to_async
                from django.db import close_old_connections

                def _auth():
                    close_old_connections()
                    try:
                        return authenticate_staff(username, password, self._remote)
                    finally:
                        close_old_connections()

                try:
                    user, reason = await sync_to_async(_auth, thread_sensitive=False)()
                except Exception as exc:  # noqa: BLE001
                    print(f"labvault-cli-ssh auth error: {exc}", flush=True)
                    return False
                if user is None:
                    print(f"labvault-cli-ssh auth denied user={username!r} reason={reason}", flush=True)
                    return False
                self._conn.labvault_user = user
                return True

            def session_requested(self):
                if active_sessions["n"] >= max_sessions:
                    return False
                active_sessions["n"] += 1
                self._counted = True
                return True

            def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
                return False  # no forwarding

            def server_requested(self, listen_host, listen_port):
                return False

        def process_factory(process):
            return handle_client(process)

        self.stdout.write(f"LabVault CLI SSH listening on {host}:{port}")
        await asyncssh.create_server(
            _Server,
            host,
            port,
            server_host_keys=[host_key],
            process_factory=process_factory,
            sftp_factory=None,
            allow_scp=False,
            login_timeout=auth_timeout,
            keepalive_interval=max(30, idle // 10),
        )
        # Idle disconnect is enforced per-session via AsyncSSH connection keepalive;
        # keep the server running forever.
        while True:
            await asyncio.sleep(3600)
