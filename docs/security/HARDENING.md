# Hardening (customer SKU)

Implemented controls:

- `validate_external_config` at install / systemd ExecStartPre
- **No** `docker.sock` / log-agent
- Idle collector/heartbeat by default
- Staff-only CLI + opsd allowlist (cannot stop `web` from browser)
- Health endpoints without inventory leakage
- Hard-dumped Capex / Hyperview / LAAS / AI Nexus / Snappi / Demo / UHD hardware
- Random bootstrap credentials by default (`/var/lib/labvault/bootstrap-credentials`); published demo logins are not the default and require `LABVAULT_DEMO_DEFAULTS=1`
- Nginx TLS termination recommended
- Compose default DB password is lab-only — rotate for shared use
- `tools/check_public_source.py` fails closed on dumped surfaces

See also [CREDENTIALS.md](CREDENTIALS.md), [THREAT_MODEL.md](THREAT_MODEL.md).
