# Quick start

1. Read [REQUIREMENTS](../install/REQUIREMENTS.md) — install OpenLDAP devel on bare metal.
2. Choose one:
   - `sudo ./deploy/install/oneshot-compose.sh`
   - `sudo ./deploy/install/oneshot-systemd.sh`
3. Read the credential file printed as `credential_file=` (default `/var/lib/labvault/bootstrap-credentials`).
4. Open `https://<host>:9443/login/` and sign in with those values (use `curl -k` until you install an operator cert). Follow [FIRST_LOGIN](FIRST_LOGIN.md).
5. As staff, open `/cli/` → run `help`, `whoami`, `diag cheap`, `fleet health`.
6. Follow [FIRST_LAB](FIRST_LAB.md) to add inventory and a topology.

Oneshot sets `LABVAULT_DEMO_DEFAULTS=1` so first login is `admin` / `labvault!`. A random file-only password is **not** the default unless you set `LABVAULT_BOOTSTRAP_RANDOM=1`. See [FIRST_LOGIN](FIRST_LOGIN.md) for where to change the flags (`.env` or `/etc/labvault/labvault.env`).

Probes: `/health/live` and `/health/ready` (no trailing slash).
