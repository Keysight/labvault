# First login

Oneshot install writes a one-time admin password and fleet token to root-owned files (mode `0600`). **Read those files** — the installer does not print the password.

```bash
sudo cat /var/lib/labvault/bootstrap-credentials
sudo cat /var/lib/labvault/fleet-token
```

(`LABVAULT_STATE_DIR` overrides that directory.)

Oneshot sets `LABVAULT_DEMO_DEFAULTS=1` so first login is `admin` / `labvault!`. A random file-only password is **not** the default unless you set `LABVAULT_BOOTSTRAP_RANDOM=1`. Rotate the demo password after first login.

| | |
|--|--|
| Login URL | `https://<host>:9443/login/` (default TLS; first-run cert is self-signed) |
| Username / password | oneshot default `admin` / `labvault!` (also written to `bootstrap-credentials`) |
| Fleet token | oneshot default name `demo-api`, value `labvault-default-api-token` (also in `fleet-token`) |

Staff CLI is at `/cli/` (`is_staff` required, else 403). Appliance SSH: `ssh -p 2222 admin@<host>` with the same password.

## Install login flags (compose, systemd, airgap)

| Flag | Oneshot default | Effect |
|------|-----------------|--------|
| `LABVAULT_DEMO_DEFAULTS=1` | **on** | First admin is `admin` / `labvault!`. First fleet token is `labvault-default-api-token`. |
| `LABVAULT_BOOTSTRAP_RANDOM=1` | off | Turns demo defaults **off**. Password and token are random, file-only. |
| `LABVAULT_BOOTSTRAP_PASSWORD` | unset | Overrides the admin password for a **new** install only. |
| `LABVAULT_FLEET_TOKEN` | unset | Overrides the first fleet Bearer value for a **new** install only. |

A second `bootstrap_labvault` run does **not** reset an existing admin or token.

### Where to set them

| When | Where |
|------|--------|
| This oneshot only | Prefix the command: `sudo LABVAULT_BOOTSTRAP_RANDOM=1 ./deploy/install/oneshot-compose.sh` |
| Compose (persists) | Repo `.env` (see `.env.example`) — `LABVAULT_DEMO_DEFAULTS=1` or `LABVAULT_BOOTSTRAP_RANDOM=1` |
| systemd / airgap (persists) | `/etc/labvault/labvault.env` (`LABVAULT_ENV_FILE` overrides the path) |
| After install (password) | UI **Password** or `manage.py changepassword admin` — then edit `bootstrap-credentials` |
| After install (fleet token) | Settings → **API Tokens** — then edit `fleet-token` |

Oneshot writes the chosen values into `.env` or `labvault.env` as `LABVAULT_DEMO_DEFAULTS=…` so later restarts match the install.

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
| Published demo values | oneshot default (`LABVAULT_DEMO_DEFAULTS=1`); random only with `LABVAULT_BOOTSTRAP_RANDOM=1` |

Lock a username out of self-service password change with `LABVAULT_PASSWORD_LOCKED_USERNAMES`. Break-glass reset (account `godmode` by default, if that user exists): `/accounts/breakglass-reset-password/`. Django `/admin/` is limited to usernames in `LABVAULT_DJANGO_ADMIN_USERNAMES` (default `godmode` only — not `admin`).
