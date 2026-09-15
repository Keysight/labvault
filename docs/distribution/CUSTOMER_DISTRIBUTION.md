# Customer distribution (public SKU)

This tree is the **LabVault customer SKU** for public distribution. It is a hard-cut product surface, not an internal build with features disabled by flags.

## Included

- Inventory, Keysight chassis, topology / Lab Pulse, reservations, fabric, fleet APIs, diagnostics (bounded), staff LabVault CLI (web + SSH), opsd allowlisted lifecycle, oneshot installers (Compose / systemd / airgap / Proxmox guest).

## Explicitly not included

Capex, Hyperview, LAAS, AI Nexus, Snappi, Demo Stage, personal design credit, UHD hardware (bfshell/ucli), docker.sock log-agent.

`tools/check_public_source.py` fails closed if dumped surfaces reappear.

## Safe defaults

| Setting | Customer default |
|---------|------------------|
| `LABVAULT_WORKER_MODE` | `idle` until restore or operator sets `live` |
| `LABVAULT_DRIVER_PLUGIN_MODE` | `off` (built-in drivers only) |
| Topology wizard / deep scan | Off |
| Bootstrap | Oneshot `admin` / `labvault!` (`LABVAULT_DEMO_DEFAULTS=1`). A random file-only password is **not** the default unless `LABVAULT_BOOTSTRAP_RANDOM=1`. |

## Restore dataset

Use a `labvault-full-export` v1 JSON (not a Postgres volume tarball). The shareable DC example is [resources/examples/dc8-pickup-export.json](../../resources/examples/dc8-pickup-export.json) (AresONE + OCS + Arista + one topology only). Edit IPs and credentials before import — the field checklist is [DC8_PICKUP](../getting-started/DC8_PICKUP.md). Also [FIRST_LAB](../getting-started/FIRST_LAB.md) and [BACKUP_RESTORE](../admin/BACKUP_RESTORE.md).

```bash
sudo LABVAULT_RESTORE_DATASET=/path/labvault_export.json \
  ./deploy/install/oneshot-compose.sh
# also supported on oneshot-systemd.sh and oneshot-airgap.sh
```

## Hardening

Implemented controls: [HARDENING.md](../security/HARDENING.md).  
Audited pre-deploy plan notes: [PRE_DEPLOY_HARDENING.md](PRE_DEPLOY_HARDENING.md).

## GitHub

Before push: [docs/install/GITHUB.md](../install/GITHUB.md).
