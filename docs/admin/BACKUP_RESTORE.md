# Backup & restore

```bash
./labvaultctl --adapter systemd backup
# → /var/lib/labvault/backups/labvault-YYYYMMDD-HHMMSS
./labvaultctl --adapter systemd restore --drill /var/lib/labvault/backups/labvault-…
```

Contents: Postgres dumps **or** sqlite copies + `MANIFEST.txt`.

Non-drill restore is intentionally blocked (maintenance window). Also export LabVault dataset/bundle for logical portability. On Proxmox, snapshot the guest before upgrades.

## Shareable DC example (not a full backup)

`labvaultctl backup` and a full `export_labvault_dataset` copy the whole appliance (every vendor, reservations, audit). Do **not** give those to a customer.

The file they can import is [resources/examples/dc8-pickup-export.json](../../resources/examples/dc8-pickup-export.json): same `labvault-full-export` v1 schema, but only 8 AresONE chassis, the OCS, four Arista spines, and one lab topology. All other sections are empty arrays.

**What to change (IPs, passwords, topology `device_ip`, what to leave alone):** [getting-started/DC8_PICKUP.md](../getting-started/DC8_PICKUP.md).

1. Edit that JSON using the table in DC8_PICKUP — every `192.0.2.*` you intend to use, plus matching topology node IPs. The UI will not retarget topology nodes if you change an IP later.
2. **DATA → Import → Import full LabVault dataset** (or `LABVAULT_RESTORE_DATASET=/path/dc8-pickup-export.json` on oneshot). Either path turns collector and heartbeat **live** so Lab Pulse and fleet stats start without a second toggle.
3. Open `/keysight/`, `/lab-topology/`, fabric, and `/usage/`.

Placeholders are RFC 5737 (`192.0.2.*`) and `changeme` / `admin`.
