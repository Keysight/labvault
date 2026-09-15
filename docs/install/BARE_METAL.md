# Bare metal / systemd implementation

## One-shot

```bash
sudo ./deploy/install/oneshot-systemd.sh                 # → /opt/labvault/current
sudo ./deploy/install/oneshot-systemd.sh /opt/labvault/current

# Optional: restore labvault-full-export JSON and enable live Pulse
sudo LABVAULT_RESTORE_DATASET=/path/labvault_export.json \
  ./deploy/install/oneshot-systemd.sh
```

Installs OS packages (including **python3.11** and **PostgreSQL 15** on Rocky/RHEL 9, plus OpenLDAP devel), creates `labvault` user, syncs tree, builds venv, writes `/etc/labvault/labvault.env`, runs `postgresql-setup --initdb` when the data directory has no `PG_VERSION`, creates Postgres DBs, installs units from `deploy/systemd/` with rewritten `WorkingDirectory`, runs `labvaultctl install`, enables workers + opsd. When `LABVAULT_RESTORE_DATASET` is set, workers start in `live` mode and oneshot runs one heartbeat + collector tick so fleet/Pulse are populated before verify.
## Unit map

| Unit | Exec |
|------|------|
| `labvault-web` | gunicorn → `127.0.0.1:8000` (nginx TLS **:9443**) |
| `labvault-refresh` | `run_labvault_refresh` |
| `labvault-heartbeat` | `run_fleet_heartbeat` |
| `labvault-collector` | `run_metric_collector` |
| `labvault-cli-worker` | `run_cli_worker` |
| `labvault-opsd` | `opsd/opsd.py` |

Environment: `/etc/labvault/labvault.env`  
State / backups: `/var/lib/labvault`  
opsd socket: `/run/labvault/ops.sock`

## labvaultctl cheat sheet

Global flags **before** subcommand:

```bash
./labvaultctl host-deps
sudo ./labvaultctl host-deps --install
./labvaultctl --adapter systemd check
./labvaultctl --adapter systemd install
./labvaultctl --adapter systemd status
./labvaultctl --adapter systemd backup
./labvaultctl --adapter systemd restore --drill /var/lib/labvault/backups/labvault-<stamp>
```

There is **no** `backup create --out`. Backup prints the directory path.

On Rocky/RHEL with SELinux Enforcing, oneshot labels **9443/tcp** as `http_port_t` so host nginx can bind the customer TLS port.

## TLS

Oneshot enables HTTPS on **:9443** and generates a lab cert if you have not set `LABVAULT_TLS_CERT`. To replace it:

```bash
sudo LABVAULT_ROOT=/opt/labvault/current \
  ./deploy/scripts/install-labvault-nginx.sh --hostname labvault.example
```

Upstream stays **127.0.0.1:8000**. See [TLS.md](TLS.md).
