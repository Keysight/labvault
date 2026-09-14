# Configuration reference

## Files

| Path | Consumers |
|------|-----------|
| Repo `.env` | Compose `env_file`, `labvaultctl` dotenv |
| `/etc/labvault/labvault.env` | systemd units |
| `$LABVAULT_STATE_DIR` (default `/var/lib/labvault`) | bootstrap creds + backups |

## Required variables

| Variable | Rules |
|----------|-------|
| `DJANGO_SECRET_KEY` | ≥32 chars; not placeholder-like |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated; no `*` |
| `DATABASE_URL` | Postgres URL (SQLite is developer/test only) |
| `NP_TIMESERIES_DATABASE_URL` | Second DB |

## Important optional

| Variable | Purpose |
|----------|---------|
| `LABVAULT_CSRF_TRUSTED_ORIGINS` | Full origins including scheme |
| `LABVAULT_PUBLIC_HOSTNAME` | FQDN for nginx / CSRF helpers |
| `LABVAULT_PUBLIC_ORIGIN` | Public origin (validate_external_config) |
| `LABVAULT_USE_TLS` | `true` behind HTTPS |
| `LABVAULT_TLS_CERT` / `LABVAULT_TLS_KEY` | Cert paths |
| `LABVAULT_OPS_SOCK` | Default `/run/labvault/ops.sock` |
| `LABVAULT_WORKER_MODE` | Workers default `idle` |
| `LABVAULT_BOOTSTRAP_PASSWORD` | First admin password (only if no users exist) |
| `LABVAULT_FLEET_TOKEN` | First fleet Bearer value (only if no token exists) |
| `LABVAULT_DEMO_DEFAULTS` | `1` = published demo login (not the default) |
| `LABVAULT_PASSWORD_LOCKED_USERNAMES` | Usernames that cannot use `/accounts/password_change/` |
| `LDAP_*` | See [LDAP.md](LDAP.md) |

Random credentials are the default. Demo logins are not the default; they require `LABVAULT_DEMO_DEFAULTS=1`.

## Compose vs host

Inside Compose use hostnames `db` / `metrics-db`. On the host OS those names do not resolve — use oneshot-compose/`docker compose exec` for management commands, or set host-reachable URLs for systemd.
