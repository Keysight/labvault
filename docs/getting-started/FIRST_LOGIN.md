# First login

Oneshot install writes a one-time admin password and fleet token to root-owned files (mode `0600`). **Read those files** — the installer does not print the password.

```bash
sudo cat /var/lib/labvault/bootstrap-credentials
sudo cat /var/lib/labvault/fleet-token
```

(`LABVAULT_STATE_DIR` overrides that directory.)

Random credentials are the default. Demo logins (`admin` / `labvault!`) are **not** the default; they require explicit `LABVAULT_DEMO_DEFAULTS=1`. Rotate any demo values immediately.

| | |
|--|--|
| Login URL | `https://<host>:9443/login/` (default TLS; first-run cert is self-signed) |
| Username / password | values in `bootstrap-credentials` |
| Fleet token | value in `fleet-token` (`Authorization: Bearer …`) |

Staff CLI is at `/cli/` (`is_staff` required, else 403).

---

## 1. Change the admin password (recommended immediately)

New password must pass Django’s rules: **at least 8 characters**, not too similar to `admin`, not a common password, not all digits.

### In the UI

1. Open `https://<host>:9443/login/` and sign in with the credential file. Accept the lab certificate warning, or install `LABVAULT_TLS_CERT`.
2. In the **bottom-left** of the sidebar, next to your username, click **Password**
   (or open `/accounts/password_change/` directly).
3. Enter the old password from the credential file, then your new secret twice.
4. Click **Update password**.
5. Update `/var/lib/labvault/bootstrap-credentials` so it does not still hold the install password.

If **Password** is missing and `/accounts/password_change/` sends you back to the dashboard, this account is listed in `LABVAULT_PASSWORD_LOCKED_USERNAMES`. Use the CLI method below, or clear that env var and restart `web`.

### From the host (always works)

Compose:

```bash
cd /path/to/labvault-public
sudo docker compose -f deploy/compose/docker-compose.yml exec web \
  python manage.py changepassword admin
```

Systemd / bare metal:

```bash
sudo -u labvault /opt/labvault/current/.venv/bin/python manage.py changepassword admin
```

Do **not** re-run `bootstrap_labvault` just to change the password — a second run does not reset an existing account.

---

## 2. Replace the fleet API token (recommended immediately)

1. Still logged in as the bootstrap admin, open **Admin → Settings** or `https://<host>:9443/settings/`.
2. Open the **API Tokens** tab.
3. Generate a new token and **copy the banner value immediately**.
4. Revoke the install token.
5. Update `/var/lib/labvault/fleet-token` (mode `0600`).

Swagger (`/api/docs/`) **Authorize** uses the same Bearer string.

---

## Closed lab vs shared host

| Situation | What to do |
|-----------|------------|
| Isolated lab, throwaway VM | Still rotate if anyone else can reach `:9443` |
| Shared or production-like | Rotate password **and** token before sharing the URL |
| Want unguessable secrets at install | default (random) — `sudo cat` the two files above |
| Custom values at install | `LABVAULT_BOOTSTRAP_PASSWORD` and `LABVAULT_FLEET_TOKEN` |
| Published demo values | only with `LABVAULT_DEMO_DEFAULTS=1` (not the default) |

Lock a username out of self-service password change with `LABVAULT_PASSWORD_LOCKED_USERNAMES`. Break-glass reset (account `godmode` by default, if that user exists): `/accounts/breakglass-reset-password/`. Django `/admin/` is limited to usernames in `LABVAULT_DJANGO_ADMIN_USERNAMES` (default `godmode` only — not `admin`).
