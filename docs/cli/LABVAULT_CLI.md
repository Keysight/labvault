# LabVault CLI

Staff appliance console for LabVault lifecycle and inventory.

## Primary interface: SSH appliance CLI

```bash
ssh -p 2222 <staff-user>@<labvault-host>
```

- Authenticates Django **staff** users (not OS accounts).
- No OS shell, no port forwarding, no SFTP/SCP.
- Mutating service commands require a `reason="..."` and an interactive `y/N` confirmation.
- Host key: `/var/lib/labvault/cli-ssh/ssh_host_ed25519_key` (persisted across recreate/upgrade).

Read generated bootstrap credentials:

```bash
sudo cat /var/lib/labvault/bootstrap-credentials
```

Oneshot sets `LABVAULT_DEMO_DEFAULTS=1` so SSH/web CLI login is `admin` / `labvault!`. A random file-only password is **not** the default unless you set `LABVAULT_BOOTSTRAP_RANDOM=1`.

## Secondary interface: Web CLI

1. Open `/login/` and sign in as staff.
2. Open `/cli/`.
3. Type a command line; the **server** parses it.
4. Mutating commands first obtain a one-time confirmation nonce, then invoke.

## HTTP API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/cli/v1/commands/` | Catalog |
| POST | `/api/cli/v1/confirm/` | Issue confirmation nonce |
| POST | `/api/cli/v1/invoke/` | Run command (`{"line":"..."}` or `{"command","args"}`) |
| GET | `/api/cli/v1/jobs/<id>/` | Async job |
| GET | `/api/cli/v1/history/` | History |

CSRF + staff session required for browser clients.

## Authorization

| Action | Requirement |
|--------|-------------|
| Read commands | Active staff |
| Service start/stop/restart | Superuser **or** `connect.control_labvault_services` |
| Settings set | Privileged (superuser or service-control permission) |

Bootstrap admin receives `connect.control_labvault_services`.

## Commands (canonical)

| Command | Mutating | Notes |
|---------|----------|-------|
| `help` | no | Catalog |
| `whoami` | no | Operator identity |
| `show devices` | no | alias: `device list` |
| `show chassis` | no | alias: `chassis list` |
| `show topologies` | no | alias: `topo list` |
| `show fleet` | no | alias: `fleet health` |
| `show settings` | no | alias: `settings list` |
| `settings set` | yes | Confirm |
| `show insights` | no | alias: `insights status` |
| `show services` | no | aliases: `service list`, `service status` |
| `service start\|stop\|restart <name\|all>` | yes | Requires `reason=` + nonce |
| `device show` / `device add` | add yes | Guided |
| `chassis show` | no | Guided |
| `reserve list` / `reserve create` | create yes | Guided |
| `topo import` | yes | Guided path check |
| `lldp refresh` | yes | Allowlisted driver verb |
| `settings get` | no | Guided |
| `audit list` | no | |
| `alerts list` | no | |
| `token list` | no | Prefix only |
| `history` | no | CLI history |
| `smoke` | no | `/health/live` + `/health/ready` |
| `diag cheap` | no | Empty-lab safe |

Stable result states: `ok`, `accepted`, `already_running`, `already_stopped`, `unsupported`, `denied`, `timeout`, `partial_failure`, `failed`, `skipped_source_guard`.

## See also

- [SERVICE_CONTROL.md](SERVICE_CONTROL.md)
- [COMMAND_REFERENCE.md](COMMAND_REFERENCE.md)
- [../admin/SERVICES.md](../admin/SERVICES.md)
