# Quick start

1. Read [REQUIREMENTS](../install/REQUIREMENTS.md) — install OpenLDAP devel on bare metal.
2. Choose one:
   - `sudo ./deploy/install/oneshot-compose.sh`
   - `sudo ./deploy/install/oneshot-systemd.sh`
3. Read the credential file printed as `credential_file=` (default `/var/lib/labvault/bootstrap-credentials`).
4. Open `https://<host>:9443/login/` and sign in with those values (use `curl -k` until you install an operator cert). Follow [FIRST_LOGIN](FIRST_LOGIN.md).
5. As staff, open `/cli/` → run `help`, `whoami`, `diag cheap`, `fleet health`.
6. Follow [FIRST_LAB](FIRST_LAB.md) to add inventory and a topology.

Random credentials are the default. Demo logins are not the default; they require `LABVAULT_DEMO_DEFAULTS=1`.

Probes: `/health/live` and `/health/ready` (no trailing slash).
