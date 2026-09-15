# LabVault documentation

Customer SKU documentation for install, day-2 ops, product modules, CLI, fleet APIs, and security.

## Install one-shots

| Method | Doc | Command |
|--------|-----|---------|
| Requirements | [install/REQUIREMENTS.md](install/REQUIREMENTS.md) | `./labvaultctl host-deps` |
| Docker Compose | [install/DOCKER.md](install/DOCKER.md) | `sudo ./deploy/install/oneshot-compose.sh` |
| Bare metal / systemd | [install/BARE_METAL.md](install/BARE_METAL.md) | `sudo ./deploy/install/oneshot-systemd.sh` |
| Proxmox guest | [install/PROXMOX.md](install/PROXMOX.md) | VM + compose or systemd oneshot |
| Air-gapped | [install/AIRGAP.md](install/AIRGAP.md) | `build-wheelhouse.sh` → `oneshot-airgap.sh` |
| Configuration | [install/CONFIGURATION.md](install/CONFIGURATION.md) | `.env` / `/etc/labvault/labvault.env` |
| TLS | [install/TLS.md](install/TLS.md) | `deploy/scripts/install-labvault-nginx.sh` |
| LDAP | [install/LDAP.md](install/LDAP.md) | OpenLDAP devel + env |
| Upgrade / rollback / uninstall | [UPGRADE](install/UPGRADE.md) · [ROLLBACK](install/ROLLBACK.md) · [UNINSTALL](install/UNINSTALL.md) | `labvaultctl` |
| GitHub submission | [install/GITHUB.md](install/GITHUB.md) | `.gitignore` + public-source gate |
| Customer distribution | [distribution/CUSTOMER_DISTRIBUTION.md](distribution/CUSTOMER_DISTRIBUTION.md) | What ships / what is omitted |
| Pre-deploy hardening status | [distribution/PRE_DEPLOY_HARDENING.md](distribution/PRE_DEPLOY_HARDENING.md) | Implemented controls checklist |

## Getting started

[QUICKSTART](getting-started/QUICKSTART.md) · [FIRST_LOGIN](getting-started/FIRST_LOGIN.md) · [CONCEPTS](getting-started/CONCEPTS.md) · [FIRST_LAB](getting-started/FIRST_LAB.md) · [DC example backup](getting-started/DC8_PICKUP.md)

## Modules

Full catalog: **[MODULES.md](MODULES.md)**

| Area | Docs |
|------|------|
| User | [user/](user/) — inventory, devices, chassis, topology, fabric, reservations, insights, reports, alerts, audit |
| Admin | [admin/](admin/) — **[services](admin/SERVICES.md)** (required workers), backup, users, runtime settings, monitoring, troubleshooting, capacity |
| CLI | [cli/](cli/) — LabVault CLI, commands, opsd service control, automation |
| API | [api/](api/) — fleet OpenAPI + auth |
| Security | [security/](security/) — hardening, credentials, ports, threat model |
| Development | [development/](development/) — local dev, tests, architecture, release |

## Controllers & URLs (memorize)

| What | Where |
|------|--------|
| Login | `https://<host>:9443/login/` — oneshot `admin` / `labvault!` (`LABVAULT_DEMO_DEFAULTS=1`); a random file-only password is **not** the default unless `LABVAULT_BOOTSTRAP_RANDOM=1` |
| Liveness | `/health/live` (**no** trailing slash) |
| Readiness | `/health/ready` |
| Staff CLI | `/cli/` |
| CLI invoke | `POST /api/cli/v1/invoke/` |
| Fleet index | `/api/fleet/` |
| Lifecycle | `./labvaultctl --adapter systemd\|compose <cmd>` |
| opsd socket | `/run/labvault/ops.sock` (no docker.sock) |

## Not in this SKU

Capex, Hyperview, LAAS reserve, AI Nexus, Snappi, Demo Stage, docker log-agent / `docker.sock`.
