# Credentials

Read install secrets from the host files. Oneshot sets `LABVAULT_DEMO_DEFAULTS=1` so first login is `admin` / `labvault!`.

| Secret | Where | Notes |
|--------|-------|--------|
| UI admin | `/var/lib/labvault/bootstrap-credentials` | Change via sidebar **Password** or `manage.py changepassword admin` |
| Fleet Bearer | `/var/lib/labvault/fleet-token` | Settings → **API Tokens** → Generate → revoke the install token |
| Django secret | `.env` / `labvault.env` | ≥32 chars; not a placeholder |
| DB passwords | compose defaults `labvault` | **Change for shared use** |
| LDAP bind | env | |
| TLS keys | `LABVAULT_TLS_*` | 0600 |

Published demo values (`admin` / `labvault!` and `labvault-default-api-token`) are the **oneshot** default (`LABVAULT_DEMO_DEFAULTS=1`). A random file-only password is **not** the default unless `LABVAULT_BOOTSTRAP_RANDOM=1`. Change flags in Compose `.env` or systemd `/etc/labvault/labvault.env`. See [getting-started/FIRST_LOGIN.md](../getting-started/FIRST_LOGIN.md).

Do not commit `.env`, sqlite DBs, or credential files.
