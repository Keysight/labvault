# `deploy/compose/`

Docker Compose stack used by `deploy/install/oneshot-compose.sh`. Guide:
[docs/install/DOCKER.md](../../docs/install/DOCKER.md).

| File | Purpose |
|---|---|
| `docker-compose.yml` | Services `db`, `metrics-db` (postgres:15), `web`, `heartbeat`, `refresh`, `jobs`, `collector`, `cli-ssh`, `nginx`. Volumes `pgdata`, `metricsdata`, `media`, `djangocache` (shared cache — web and workers must share it), `cli_ssh_hostkeys` |
| `Dockerfile` | App image (python:3.11-slim + LDAP/libpq build deps, `requirements.txt`, gunicorn) used by every app service |
| `entrypoint.sh` | Fixes volume ownership, migrates both databases, runs `collectstatic` only for gunicorn, then drops to user `labvault` (uid 10001) keeping supplementary groups |
| `nginx-tls.conf` | Default edge: TLS on `:9443` → `web:8000`, certs from `${LABVAULT_TLS_DIR}` |
| `nginx-http.conf` | HTTP-only edge for debugging (not used by default) |

Ports published to the host: `${LABVAULT_TLS_PORT:-9443}` (nginx),
`127.0.0.1:${LABVAULT_HTTP_PORT:-8000}` (web, loopback only), `${LABVAULT_CLI_SSH_PORT:-2222}`
(cli-ssh).

The host directory `${LABVAULT_OPS_SOCK_HOST:-/run/labvault}` is mounted read-only into
`web`, `jobs`, and `cli-ssh` so they can reach the host opsd socket. Only `cli-ssh` joins the
`labvault-ops` group (`group_add: ${LABVAULT_OPS_GID}`). No Docker socket is mounted.

The Postgres containers use fixed credentials on the private Compose network; change them in
both `docker-compose.yml` and `.env` if the host is shared.
