# Rollback

```bash
./labvaultctl --adapter systemd restore --drill /var/lib/labvault/backups/labvault-<stamp>
```

Full data restore is a maintenance-window operation (ctl refuses non-drill restore by design). Prefer Proxmox/VM snapshots for whole-guest revert.
