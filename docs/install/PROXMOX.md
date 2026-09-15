# Proxmox guest

Provision a Linux VM, then run a LabVault oneshot **inside the guest**. There is no LabVault Proxmox plugin.

Two supported guest layouts:

- **A — Compose:** install Docker in the guest, then `sudo ./deploy/install/oneshot-compose.sh` ([DOCKER.md](DOCKER.md))
- **B — systemd:** install host packages, then `sudo ./deploy/install/oneshot-systemd.sh` ([BARE_METAL.md](BARE_METAL.md))

```bash
# Guest
sudo ./deploy/install/oneshot-compose.sh
# or with inventory + Lab Pulse:
sudo LABVAULT_RESTORE_DATASET=/path/to/labvault_export.json \
  ./deploy/install/oneshot-compose.sh
```

Minimum sizing: 4 vCPU, 8 GiB RAM, 40 GiB free disk. Open **9443/tcp** for the default TLS UI.

Give the guest a **static IPv4** (`ipconfig0=ip=<addr>/<prefix>,gw=<gateway>`) plus nameservers if DHCP or the QEMU guest agent is unreliable. Cloud-init `package_upgrade` can stall first boot — leave it off for oneshot guests.

Generic cloud-init notes live under `deploy/proxmox/`.
