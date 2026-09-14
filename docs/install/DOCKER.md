# Docker Compose

Create a Linux host with Docker Engine 24+ and Compose v2.20+, then run the oneshot.

On Proxmox, create a guest first: [PROXMOX.md](PROXMOX.md).

## One-shot

```bash
cd /path/to/labvault-public
sudo ./deploy/install/oneshot-compose.sh
```

Read `credential_file=` from the READY banner.

## One-shot with live lab restore (Lab Pulse on)

```bash
sudo LABVAULT_RESTORE_DATASET=/path/to/labvault_export.json \
  ./deploy/install/oneshot-compose.sh
```

Empty installs stay idle unless you set `LABVAULT_WORKER_MODE=live`.

## Architecture

| Service | Image | Role |
|---------|-------|------|
| `db` | `postgres:15` | Primary DB (`labvault`) |
| `metrics-db` | `postgres:15` | Timeseries (`labvault_metrics`) |
| `web` | build `deploy/compose/Dockerfile` | Gunicorn loopback `:8000` + entrypoint migrate |
| `refresh` | same | `run_labvault_refresh` |
| `jobs` | same | `run_cli_worker` |
| `heartbeat` | same | `run_fleet_heartbeat` (idle by default) |
| `collector` | same | `run_metric_collector` (idle by default) |
| `cli-ssh` | same | Appliance SSH CLI `:2222` |
| `nginx` | `nginx:1.27-alpine` | TLS edge **:9443** → `web:8000` |

`opsd` runs on the **host** (`labvault-opsd.service`). Containers never mount `docker.sock`.

## Database URLs

Compose injects into app containers:

```
DATABASE_URL=postgres://labvault:labvault@db:5432/labvault
NP_TIMESERIES_DATABASE_URL=postgres://labvault:labvault@metrics-db:5432/labvault_metrics
```

Default password `labvault` is for **lab installs only**. Change for any shared network and rotate volumes.

## Manual commands

```bash
cp -n .env.example .env   # set DJANGO_SECRET_KEY
mkdir -p /run/labvault
docker compose -f deploy/compose/docker-compose.yml up -d --build
curl -kfsS https://127.0.0.1:9443/health/ready
./labvaultctl --adapter compose status
```

## Verify checklist

- [ ] `curl -kfsS https://127.0.0.1:9443/health/live`
- [ ] `curl -kfsS https://127.0.0.1:9443/health/ready`
- [ ] Login at `/login/` using `/var/lib/labvault/bootstrap-credentials`
- [ ] Rotate password and fleet token ([FIRST_LOGIN](../getting-started/FIRST_LOGIN.md))
- [ ] Staff `/cli/` loads; `diag cheap` → ok
- [ ] `docker compose … ps` shows healthy `web`, `refresh`, `jobs`
