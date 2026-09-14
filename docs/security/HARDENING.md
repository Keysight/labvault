# Hardening (customer SKU)

Implemented controls:

- `validate_external_config` at install / systemd ExecStartPre
- **No** `docker.sock` / log-agent
- Idle collector/heartbeat by default
- Staff-only CLI + opsd allowlist (cannot stop `web` from browser)
- Health endpoints without inventory leakage
- Hard-dumped Capex / Hyperview / LAAS / AI Nexus / Snappi / Demo / UHD hardware
- Oneshot bootstrap is `admin` / `labvault!` via `LABVAULT_DEMO_DEFAULTS=1` (written to `/var/lib/labvault/bootstrap-credentials`). A random file-only password is **not** the default unless `LABVAULT_BOOTSTRAP_RANDOM=1`. Rotate before sharing the host.
- Nginx TLS on **:9443** by default (generated lab cert; replace on shared hosts)
- Compose default DB password is lab-only — rotate for shared use
- `tools/check_public_source.py` fails closed on dumped surfaces

See also [CREDENTIALS.md](CREDENTIALS.md), [THREAT_MODEL.md](THREAT_MODEL.md).
