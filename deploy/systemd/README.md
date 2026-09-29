# `deploy/systemd/`

Unit files for systemd and air-gapped installs. `oneshot-systemd.sh`, `oneshot-airgap.sh`,
and `labvaultctl install` copy them to `/etc/systemd/system/`, replacing
`/opt/labvault/current` with the real install root. All app units run as `labvault`, read
`/etc/labvault/labvault.env`, and restart on failure.

| Unit | Runs | Notes |
|---|---|---|
| `labvault-web.service` | `gunicorn labvault.wsgi:application --bind 127.0.0.1:8000 --workers 3` | `ExecStartPre=manage.py validate_external_config`; `LABVAULT_DISABLE_INPROCESS_REFRESH=1` |
| `labvault-heartbeat.service` | `manage.py run_fleet_heartbeat` | Fleet heartbeat loop |
| `labvault-collector.service` | `manage.py run_metric_collector` | Pulse metrics loop |
| `labvault-refresh.service` | `manage.py run_labvault_refresh` | Keysight card/port refresh loop |
| `labvault-cli-worker.service` | `manage.py run_cli_worker` | `CliJob` worker (logical name `jobs`) |
| `labvault-cli-ssh.service` | `manage.py run_cli_ssh` | SSH CLI on 2222; `SupplementaryGroups=labvault-ops`; state dir `/var/lib/labvault/cli-ssh` (0700) |
| `labvault-opsd.service` | `opsd/opsd.py --sock /run/labvault/ops.sock` | Runs as root; `Alias=labvault-opsd-compose.service`. Used on every deploy mode |
| `labvault-opsd-compose.service` | same broker, Compose adapter | Legacy compatibility stub; installers delete it because it conflicts with the alias |

See [operations.md](../../docs/development/subsystems/operations.md#service-table) for
intervals and what each process reads and writes.
