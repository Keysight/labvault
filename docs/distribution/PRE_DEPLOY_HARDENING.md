# Pre-deploy hardening (customer SKU status)

**Status:** Customer SKU ships the **implemented** controls below. Full Phase 0–3 of the internal hardening plan is not required for this distribution cut; external plugin Gate 4 is omitted.

## Locked decisions reflected in this tree

| Track | Customer SKU behavior |
|-------|------------------------|
| Flags | Workers **idle** by default; driver plugin mode **off**; topology wizard off |
| Logs | **No** compose `log-agent` / `docker.sock` |
| Topology | Staff paths only; no auto-seed / Google demo branding |
| Drivers | Built-in only unless `LABVAULT_DRIVER_PLUGIN_MODE` opted in |
| External-ready | `validate_external_config` rejects weak secrets, `ALLOWED_HOSTS=*`, DEBUG on public installs |
| Backup/restore | `labvaultctl backup` / `restore --drill`; oneshot `LABVAULT_RESTORE_DATASET` JSON export |
| Ops boundary | opsd allowlist; browser CLI cannot restart `web`; SSH CLI may |

## Operator checklist before production

1. Strong `DJANGO_SECRET_KEY` (≥32, not placeholder).
2. Rotate bootstrap admin password and fleet API token ([FIRST_LOGIN[../getting-started/FIRST_LOGIN.md)).
3. Prefer Postgres URLs (Compose oneshot already uses in-stack Postgres).
4. TLS is default on **:9443**; do not publish gunicorn `:8000` or `:2222` to untrusted networks without controls.
5. Run `./deploy/scripts/post_deploy_verify.sh` after install.

See also [HARDENING.md](../security/HARDENING.md) and [CUSTOMER_DISTRIBUTION.md](CUSTOMER_DISTRIBUTION.md).
