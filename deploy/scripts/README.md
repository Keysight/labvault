# `deploy/scripts/`

Helpers called by the oneshots and by operators after install.

| File | Purpose |
|---|---|
| `ensure-labvault-tls.sh` | Generate a self-signed cert/key (SAN: localhost, hostname, 127.0.0.1, primary IP) at `LABVAULT_TLS_CERT` / `LABVAULT_TLS_KEY` if missing |
| `install-labvault-nginx.sh` | Render `deploy/nginx/labvault-https.conf.template` into `/etc/nginx/conf.d/labvault-9443.conf`, label the TLS port for SELinux when enforcing, and reload nginx |
| `install-labvault-http80.sh` | Install the host `:80`/`:443` edge (`labvault-edge.conf`); skip with `LABVAULT_SKIP_HTTP80=1` |
| `apply-production-tls.sh` | Prints where TLS is configured (oneshots); changes nothing |
| `post_deploy_verify.sh` | `ADAPTER=compose\|systemd`: services/units up, opsd socket, SSH port, heartbeat plumbing, then `fleet_api_smoke.sh`. Prints `VERIFY_OK` or `VERIFY_FAILED` |
| `fleet_api_smoke.sh` | `BASE_URL`, `TOKEN`: health, login, and every `/api/fleet/*` read endpoint; optional per-chassis / OCS checks via `LABVAULT_SMOKE_CHASSIS_ID`, `LABVAULT_SMOKE_OCS_IP` |
| `cli_ssh_smoke.py` | AsyncSSH client: logs in as `LABVAULT_CLI_USER` / `LABVAULT_CLI_PASSWORD` and runs `whoami`, `help`, `show services`, `show fleet`. Host key is not verified unless `LABVAULT_CLI_KNOWN_HOSTS` is set |
| `cli_acceptance_run.sh` | `TARGET=compose\|systemd\|airgap\|proxmox-restore`: run the oneshot, verify, and log to `docs/release/acceptance/` |

Scripts read the fleet token from `$LABVAULT_STATE_DIR/fleet-token` when `TOKEN` is unset.
