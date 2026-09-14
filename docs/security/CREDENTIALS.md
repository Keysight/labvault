# Credentials

Read install secrets from the host files. Random credentials are the default.

| Secret | Where | Notes |
|--------|-------|--------|
| UI admin | `/var/lib/labvault/bootstrap-credentials` | Change via sidebar **Password** or `manage.py changepassword admin` |
| Fleet Bearer | `/var/lib/labvault/fleet-token` | Settings → **API Tokens** → Generate → revoke the install token |
| Django secret | `.env` / `labvault.env` | ≥32 chars; not a placeholder |
| DB passwords | compose defaults `labvault` | **Change for shared use** |
| LDAP bind | env | |
| TLS keys | `LABVAULT_TLS_*` | 0600 |

Published demo values (`admin` / `labvault!` and `labvault-default-api-token`) are **not** the default. They require `LABVAULT_DEMO_DEFAULTS=1`. See [getting-started/FIRST_LOGIN.md](../getting-started/FIRST_LOGIN.md).

Do not commit `.env`, sqlite DBs, or credential files.
