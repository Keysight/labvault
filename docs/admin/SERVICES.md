# Services

LabVault needs a small set of long-running processes. **Logical names are identical on Compose, systemd, and airgap** (`show services` / opsd). Adapter columns map those names onto compose services or systemd units.

Oneshot installs start the required set for that mode. If something is missing after install, start the rows below.

## Required for a working UI + fleet Pulse

| Logical name | What it does | Compose service | systemd unit | Idle vs live |
|--------------|----------------|-----------------|--------------|--------------|
| **db** | Inventory Postgres | `db` | `postgresql` (host) | Always |
| **metrics-db** | Pulse / timeseries Postgres | `metrics-db` | same Postgres, DB `labvault_metrics` | Always |
| **web** | Gunicorn + UI + fleet APIs | `web` | `labvault-web` | Always |
| **heartbeat** | Fleet heartbeat store | `heartbeat` | `labvault-heartbeat` | Process always on; probes when `LABVAULT_WORKER_MODE=live` |
| **collector** | Topology metric samples (Lab Pulse) | `collector` | `labvault-collector` | Same as heartbeat |

Without **heartbeat**, fleet health shows empty CPU/heartbeat fields.  
Without **collector** (and a migrated metrics DB), topology insights stay empty.  
Without the **shared Django cache volume/dir**, web cannot see heartbeat writes.

## Optional / host-side

| Logical name | Compose | systemd | Notes |
|--------------|---------|---------|-------|
| **cli-ssh** | `cli-ssh` (:2222) | `labvault-cli-ssh` | Appliance CLI over SSH (staff auth, no OS shell) |
| **opsd** | host unit `labvault-opsd` | `labvault-opsd` | Unix socket lifecycle broker; same unit name on every mode |
| **refresh** | — | `labvault-refresh` | Device refresh loop (bare metal) |
| **jobs** | — | `labvault-cli-worker` | Async CLI jobs (bare metal) |
| **nginx** | `nginx` | `nginx` | TLS **:9443** → gunicorn `:8000`; optional `:80` redirect |

Aliases accepted by CLI/opsd (always resolved to the logical name): `metrics_db` → `metrics-db`, `cli_ssh` → `cli-ssh`, `cli-worker` → `jobs`.

Appliance login after install:

```bash
sudo cat /var/lib/labvault/bootstrap-credentials
ssh -p 2222 <staff-user>@<host>
# UI: https://<host>:9443/login/
```

## Compose (customer SKU)

```bash
sudo ./deploy/install/oneshot-compose.sh
```

```bash
docker compose -f deploy/compose/docker-compose.yml ps
```

### Compose checklist

| Logical name | Healthy when |
|--------------|----------------|
| `db` / `metrics-db` | `healthy` in `compose ps` |
| `web` | `GET /health/ready` → 200 |
| `heartbeat` / `collector` | Up; Pulse data after live mode ticks |
| `cli-ssh` | Listening on :2222 |
| `opsd` | `labvault-opsd.service` active; `/run/labvault/ops.sock` present |

## systemd / airgap

```bash
sudo ./deploy/install/oneshot-systemd.sh
# or oneshot-airgap.sh
systemctl status labvault-web labvault-heartbeat labvault-collector labvault-opsd
```

## Browser entry (all modes)

The customer URL is `https://<host>:9443/login/`. Optional host nginx on **:80** redirects to that origin. Gunicorn on `:8000` is loopback only.
