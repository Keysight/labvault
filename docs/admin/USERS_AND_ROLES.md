# Users and roles

| Mechanism | Notes |
|-----------|--------|
| Local Django users | Bootstrap `admin` from `/var/lib/labvault/bootstrap-credentials` — rotate on first login ([../getting-started/FIRST_LOGIN.md](../getting-started/FIRST_LOGIN.md)) |
| Superuser / staff | CLI requires staff |
| LDAP | Optional; see [../install/LDAP.md](../install/LDAP.md) |
| Lock / break-glass | Env lists `LABVAULT_PASSWORD_*`, `LABVAULT_BREAKGLASS_*` |

Password: sidebar **Password** (`/accounts/password_change/`) unless the username is in `LABVAULT_PASSWORD_LOCKED_USERNAMES`. CLI fallback: `manage.py changepassword admin`.

API tokens: `/settings/` → **API Tokens**. Generate a new token, copy the banner value, revoke the install token.

Django `/admin/` is not available to `admin` by default (`LABVAULT_DJANGO_ADMIN_USERNAMES` defaults to `godmode`).
