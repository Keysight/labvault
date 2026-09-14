# Deploy matrix notes

| Method | Result | Lesson encoded in tree |
|--------|--------|------------------------|
| Customer Compose | PASS with oneshot | Postgres in-stack; entrypoint migrate; no docker.sock |
| Systemd / labvaultctl | PASS with OpenLDAP devel | `oneshot-systemd.sh` + units on `:8000` |
| Airgap | PASS with wheelhouse | `build-wheelhouse.sh` + offline pip |
| Customer Compose + live restore | PASS on a disposable staging guest | Inventory import + Pulse live; fleet GETs 200 |
| LabVault CLI | PASS | `/login/`, `/cli/`, `/api/cli/v1/invoke/` (`whoami`, `diag cheap`, `fleet health`) |

### Pitfalls fixed in docs/scripts

1. Missing `openldap-devel` → python-ldap build failure
2. Trailing slash on `/health/live/` → 404 (use no slash)
3. `/accounts/login/` → 404 (use `/login/`)
4. Nginx upstream mismatch vs app `:8000` → **templates now use 8000**
5. Weak `DJANGO_SECRET_KEY` → `validate_external_config` fails
6. Compose without migrate entrypoint → first boot flaky (**entrypoint added**)
7. Re-running bootstrap must not reset an existing admin password
