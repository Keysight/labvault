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
| `LABVAULT_USE_TLS` | Default `true` |
| `LABVAULT_TLS_PORT` | Default `9443` |
| `LABVAULT_TLS_CERT` / `LABVAULT_TLS_KEY` | Default `/var/lib/labvault/tls/` (generated on oneshot) |
| `LABVAULT_OPS_SOCK` | Default `/run/labvault/ops.sock` |
| `LABVAULT_WORKER_MODE` | Workers default `idle` |
| `LABVAULT_BOOTSTRAP_PASSWORD` | First admin password (only if no users exist) |
| `LABVAULT_FLEET_TOKEN` | First fleet Bearer value (only if no token exists) |
| `LABVAULT_DEMO_DEFAULTS` | Oneshot default `1` = `admin` / `labvault!`. A random file-only password is **not** the default unless `LABVAULT_BOOTSTRAP_RANDOM=1`. Set in `.env` (Compose) or `/etc/labvault/labvault.env` (systemd). |
| `LABVAULT_BOOTSTRAP_RANDOM` | `1` = random password + token at first boot (file-only) |
| `LABVAULT_PASSWORD_LOCKED_USERNAMES` | Usernames that cannot use `/accounts/password_change/` |
| `LDAP_*` | See [LDAP.md](LDAP.md) |

Oneshot sets `LABVAULT_DEMO_DEFAULTS=1` so first login is `admin` / `labvault!`. A random file-only password is **not** the default unless you set `LABVAULT_BOOTSTRAP_RANDOM=1`. Details and where to change them: [FIRST_LOGIN](../getting-started/FIRST_LOGIN.md).

## Compose vs host

Inside Compose use hostnames `db` / `metrics-db`. On the host OS those names do not resolve — use oneshot-compose/`docker compose exec` for management commands, or set host-reachable URLs for systemd.
