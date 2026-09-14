# Service control (opsd)

`opsd/opsd.py` listens on `LABVAULT_OPS_SOCK` (default `/run/labvault/ops.sock`).

It is the **only** privileged boundary for appliance lifecycle. Application containers never receive `docker.sock`.

Logical service names are **identical** on Compose, systemd, and airgap.

## Protocol

JSON line requests:

```json
{"action":"list"}
{"action":"status","name":"heartbeat"}
{"action":"restart","name":"heartbeat","source":"ssh","reason":"stale telemetry","request_id":"..."}
```

Allowed actions: `list`, `status`, `start`, `stop`, `restart`.  
Extra fields are rejected. Shell / arbitrary argv are never used.

## Capability matrix

| Logical service | Systemd unit | Compose service | Status | Start/stop | Restart | Notes |
|---|---|---|---|---|---|---|
| `web` | `labvault-web` | `web` | yes | SSH start only; stop denied | SSH yes | Web cannot disrupt itself |
| `heartbeat` | `labvault-heartbeat` | `heartbeat` | yes | yes | yes | |
| `collector` | `labvault-collector` | `collector` | yes | yes | yes | |
| `refresh` | `labvault-refresh` | `refresh` | yes | yes | yes | |
| `jobs` | `labvault-cli-worker` | `jobs` | yes | yes | yes | |
| `cli-ssh` | `labvault-cli-ssh` | `cli-ssh` | yes | denied from SSH | Web restart only | Avoid self-termination |
| `opsd` | `labvault-opsd` | host unit (same name) | yes | denied | denied | Broker cannot control itself |
| `nginx` | `nginx` | `nginx` (Compose) or host unit | when installed | denied | privileged | Not in restart-all |
| `db` | PostgreSQL | `db` | yes | denied | denied | Status-only |
| `metrics-db` | PostgreSQL | `metrics-db` | yes | denied | denied | Status-only |

Aliases: `metrics_db`→`metrics-db`, `cli_worker`→`jobs`, `opsd-compose`→`opsd`.

## Units

Every deploy mode uses **`labvault-opsd.service`**. Legacy `labvault-opsd-compose.service` is migrated away by oneshot-compose; status still accepts it as a fallback.
