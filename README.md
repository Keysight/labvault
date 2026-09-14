# LabVault

Lab infrastructure inventory, topology, reservation, telemetry, diagnostics, and operations.

**Customer SKU** — Capex, Hyperview, LAAS, AI Nexus, Snappi, and Demo Stage are **not** included.

Community support is best-effort. There is no product SLA unless Keysight publishes one for a given release.

## Documentation

- **[docs/INDEX.md](docs/INDEX.md)** — master index
- **[docs/MODULES.md](docs/MODULES.md)** — every module

## One-shot install

| Method | Command |
|--------|---------|
| Docker Compose | `sudo ./deploy/install/oneshot-compose.sh` |
| Bare metal / systemd | `sudo ./deploy/install/oneshot-systemd.sh` |
| Air-gapped | `./deploy/install/build-wheelhouse.sh` then `sudo ./deploy/install/oneshot-airgap.sh <wheelhouse>` |
| Proxmox guest | Create a Linux VM, then oneshot — [docs/install/PROXMOX.md](docs/install/PROXMOX.md) |

```bash
./labvaultctl host-deps          # show OS packages
./labvaultctl --help             # lifecycle controller

# Restore a working lab export and turn Lab Pulse on in one shot:
sudo LABVAULT_RESTORE_DATASET=/path/to/labvault_export.json \
  ./deploy/install/oneshot-compose.sh
```

Critical bare-metal packages: `openldap-devel` (Rocky) or `libldap2-dev` (Debian).

## After install

| URL | Purpose |
|-----|---------|
| `http://<host>:8000/login/` | UI login — read `/var/lib/labvault/bootstrap-credentials` |
| `http://<host>:8000/health/ready` | Ready probe (**no** trailing slash) |
| `http://<host>:8000/cli/` | Staff LabVault CLI |

Oneshot writes mode `0600` copies under `/var/lib/labvault/` (`bootstrap-credentials`, `fleet-token`). Random credentials are the default. Demo logins are **not** the default; they require `LABVAULT_DEMO_DEFAULTS=1`.

First-login steps: [docs/getting-started/FIRST_LOGIN.md](docs/getting-started/FIRST_LOGIN.md).

## Security

[SECURITY.md](SECURITY.md) · [docs/security/HARDENING.md](docs/security/HARDENING.md)

No `docker.sock`. App listens on **:8000** (nginx upstream must match).
