# Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `python-ldap` build fails | Missing OpenLDAP devel | `labvaultctl host-deps --install` |
| `validate_external_config` fails | Weak/placeholder secret | `secrets.token_urlsafe(48)` in env |
| `/health/live/` → 404 | Trailing slash | Use `/health/live` |
| `/accounts/login/` → 404 | Wrong path | `/login/` |
| Password change redirects to dashboard | Username in `LABVAULT_PASSWORD_LOCKED_USERNAMES` | `manage.py changepassword` or clear the env var and restart `web` |
| Fleet API 401 after rotate | Still sending `labvault-default-api-token` | Use the new token from Settings → API Tokens; revoke leftover `demo-api` |
| nginx 502 | Upstream port wrong | Must be `127.0.0.1:8000` |
| Browser TLS warning | Lab self-signed cert | Expected on first run; install `LABVAULT_TLS_CERT` or `curl -k` |
| `https://host:9443` refused | nginx TLS edge down | `docker compose ps nginx` / `systemctl status nginx`; certs in `/var/lib/labvault/tls` |
| `opsd_unavailable` | No opsd socket | Enable `labvault-opsd` or ignore on compose-only |
| Compose DB errors from host ctl | Hostname `db` | Use `docker compose exec web …` |
| `GET /api/diagnostics.json` → 500 | Live driver `probe()` hangs (e.g. SONiC REST) | Use `/api/fleet/health.json`; do not treat diagnostics export as a liveness probe |
| Pulse / fleet telemetry all `null` | Compose `web` and `heartbeat` used separate container filesystems for `FileBasedCache`; root-owned cache files from `docker compose exec` blocked gunicorn (`labvault`) | Shared `djangocache` volume on `web`/`heartbeat`/`collector`; set `LABVAULT_CACHE_DIR`; run `manage.py` as `-u labvault`; see `deploy/scripts/fleet_api_smoke.sh` |
| Fleet `missed_heartbeat` on (almost) every chassis | Heartbeat lock `/tmp/labvault_fleet_heartbeat.lock` recreated as `root:root` by `docker compose exec` (no `-u labvault`); worker then loops `Permission denied` and never refreshes the store | `rm` the root-owned lock (or restart so entrypoint removes it); lock now prefers `/app/var/django_cache/fleet_heartbeat.lock`; always `docker compose exec -u labvault …` |
| Lab Pulse / topology insights empty | Collector not running; `collect_all_topologies` import missing; or `NP_TIMESERIES_DATABASE_URL` ignored because `dj_database_url.config()` preferred `DATABASE_URL` | Ensure `collector` is up (`docs/admin/SERVICES.md`); settings use `dj_database_url.parse(url)`; migrate `--database np_timeseries` |
| `/keysight/` “No cards detected” while fleet is green | Card grid waited on a blocking IxOS `get_cards`/SSH fetch; gunicorn does not run that poller; pickup JSON has inventory only | Heartbeat writes `/ports` into the same cache; detail page hydrates cards from that list; `labvault-refresh` now does the full fetch. Auth-failed chassis stay empty. |

CLI: `diag cheap`. UI: staff Diagnostics Center at `/diagnostics/` (export + bundle from that page).
