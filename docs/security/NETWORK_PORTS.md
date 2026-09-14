# Network ports

| Port | Direction | Service |
|------|-----------|---------|
| **9443** | inbound | nginx TLS (customer default) → gunicorn `:8000` |
| 80 | inbound | optional nginx redirect to `https://<host>:9443` |
| 8000 | loopback | gunicorn / Compose `web` (not published on all interfaces) |
| 2222 | inbound | LabVault appliance SSH CLI (staff auth, no OS shell) |
| 5432 | private | Postgres |

opsd uses a **Unix socket** (`/run/labvault/ops.sock`), not TCP.

## Browser URLs

- `https://<host>:9443/login/`
- `https://<host>:9443/health/ready`
- `https://<host>:9443/cli/`

First-run uses a generated certificate. `curl` needs `-k` until you install `LABVAULT_TLS_CERT`. Skip the optional `:80` redirect with `LABVAULT_SKIP_HTTP80=1`. Skip TLS only with `LABVAULT_SKIP_TLS=1` (debug).
