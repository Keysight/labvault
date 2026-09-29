# `connect/labvault_cli/`

Staff appliance CLI shared by the browser console (`/cli/`), the JSON API
(`/api/cli/v1/`), and the SSH REPL (port 2222). Design, tiers, nonce flow, and a
step-by-step guide to adding commands: [docs/development/subsystems/cli.md](../../docs/development/subsystems/cli.md).

| File | Purpose |
|---|---|
| `__init__.py` | Imports `commands` so every handler registers; re-exports `COMMANDS`, `command`, `get_command` |
| `registry.py` | `Command` dataclass, `@command(...)` decorator, `COMMANDS` / `ALIASES`, `get_command()` |
| `commands/__init__.py` | All command handlers (`help`, `show ...`, `settings set`, `device ...`, `reserve ...`, `service start/stop/restart`, `diag cheap`, ...) |
| `parser.py` | `parse_line()` (shlex, longest-match names, positional + `key=value` args) and `parse_invoke_body()` |
| `runner.py` | `CliContext`, `invoke()`, `prepare_confirm()`, one-time nonce store, `can_control_services()`, `CliInvocation` audit |
| `redact.py` | `redact_payload()` — masks secret-looking keys and URL userinfo |
| `ops_client.py` | Client for the opsd Unix socket (`/run/labvault/ops.sock`) |
| `service_catalog.py` | Re-exports `opsd/service_catalog.py` so Django and opsd share one catalog |
| `ssh_auth.py` | CIDR allowlist, `CliAuthThrottle` backoff/lockout, staff password auth |
| `ssh_repl.py` | `ApplianceSSHSession` line REPL and AsyncSSH `handle_client` callback |

Views live in [`connect/labvault_cli_views.py`](../labvault_cli_views.py); the SSH server is
the `run_cli_ssh` management command. Tests: `connect/tests/test_labvault_cli_*.py`,
`test_cli_ssh_auth.py`, `test_redact_payload.py`, `test_opsd_broker.py`.
