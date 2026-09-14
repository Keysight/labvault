# Changelog

## Unreleased (customer SKU)

- Default customer URL is HTTPS **:9443** (nginx TLS). Gunicorn stays on loopback `:8000`.

- Oneshot installers: compose, systemd, airgap + wheelhouse builder
- `LABVAULT_RESTORE_DATASET` restores a live lab and turns Lab Pulse on in one shot
- Compose workers read `LABVAULT_WORKER_MODE` from `.env` (`env_file`); entrypoint chowns named volumes then drops to uid 10001
- Dataset import ignores stale JSON foreign-key PKs (google-demo BMC rows)
- Fleet/OCS snapshot APIs accept Bearer tokens; CLI `device list` / `diag cheap` / `fleet health` report live inventory
- `.gitignore` + `.gitattributes` (LF) for Keysight GitHub submission
- Nginx upstream aligned to gunicorn `:8000`
- Compose entrypoint runs migrate/collectstatic
- Hard-dumped LAAS HW APIs, AI Nexus, agent-research handlers → 404 JSON
- Removed internal plans / DEVELOPMENT_CONTEXT from tree
- Documentation expanded under `docs/` (INDEX + MODULES + install/user/admin/cli/api/security)
- `check_public_source.py` content gates strengthened
