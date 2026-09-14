# Backup & restore

```bash
./labvaultctl --adapter systemd backup
# → /var/lib/labvault/backups/labvault-YYYYMMDD-HHMMSS
./labvaultctl --adapter systemd restore --drill /var/lib/labvault/backups/labvault-…
```

Contents: Postgres dumps **or** sqlite copies + `MANIFEST.txt`.

Non-drill restore is intentionally blocked (maintenance window). Also export LabVault dataset/bundle for logical portability. On Proxmox, snapshot the guest before upgrades.
