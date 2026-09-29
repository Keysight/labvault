# `deploy/`

Everything needed to install and run a LabVault appliance. Start with a oneshot in
[`install/`](install/README.md); the rest are the pieces it wires together.

Architecture (process model, ports, filesystem layout, bootstrap):
[docs/development/subsystems/operations.md](../docs/development/subsystems/operations.md).
Install guides: [docs/install/](../docs/install/).

| Directory | Contents |
|---|---|
| [`install/`](install/README.md) | Oneshot installers (Compose, systemd, air-gapped), wheelhouse builder, shared helpers |
| [`compose/`](compose/README.md) | `docker-compose.yml`, app image `Dockerfile`, container entrypoint, Compose nginx configs |
| [`systemd/`](systemd/README.md) | Unit files for web, workers, SSH CLI, and opsd |
| [`nginx/`](nginx/README.md) | Host nginx configs and templates for the TLS edge |
| [`scripts/`](scripts/README.md) | TLS helpers, nginx installers, post-deploy verify, API / CLI smoke tests |
| [`proxmox/`](proxmox/README.md) | Notes for running a oneshot inside a Proxmox guest |

The lifecycle tool for day-2 work (backup, restore, update, rollback, status) is
[`../labvaultctl`](../labvaultctl). The top-level `labvault.service` is a deprecated stub;
do not install it.
