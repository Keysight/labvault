# Network ports

| Port | Direction | Service |
|------|-----------|---------|
| 443 | inbound | nginx TLS edge (operator cert) → gunicorn `:8000` |
| 80 | inbound | optional nginx redirect to HTTPS or `:8000` |
| 8000 | inbound | gunicorn / Compose `web` (direct access still works) |
| 8080 | inbound | optional Compose `nginx` HTTP edge (`LABVAULT_NGINX_PORT`) |
| 2222 | inbound | LabVault appliance SSH CLI (staff auth, no OS shell) |
| 5432 | private | Postgres |

opsd uses a **Unix socket** (`/run/labvault/ops.sock`), not TCP.

## Bare IP in the browser

On every oneshot, `deploy/scripts/install-labvault-http80.sh` can install a host nginx edge so:

- `http://<host>/` → `http://<host>:8000/login/` (or HTTPS when TLS is configured)

Skip the edge with `LABVAULT_SKIP_HTTP80=1`.
