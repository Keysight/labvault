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

## Find VM name, IP, and login without an AI agent

The guest address is **not** stored in this git tree. Read it from Proxmox or the guest.

| Where | What you get |
|-------|----------------|
| Proxmox UI → node → VM → **Summary** | Name, status, memory. IP only if QEMU guest agent is running. |
| Same VM → **Cloud-Init** | `ipconfig0` (static `ip=` / `gw=`), nameserver, ciuser |
| Same VM → **Hardware** | vCPU, RAM, disk |
| Hypervisor shell | `qm list` · `qm config <vmid>` · `qm guest cmd <vmid> network-get-interfaces` |
| Inside the guest | `ip -4 addr` · `hostname -I` |
| After oneshot | READY banner; `sudo cat /var/lib/labvault/bootstrap-credentials` |

Customer UI is `https://<guest-ipv4>:9443/login/`. Gunicorn stays on loopback `:8000`.

Prefer a **static** `ipconfig0` when DHCP or the guest agent is missing — otherwise the VM can be running with no reachable address.
