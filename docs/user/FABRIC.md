# Fabric & port fabric

Fabric views hang off a lab topology:

- Fabric map
- Port fabric
- Fabric snapshots

## OCS patch snapshots

When an OCS (optical circuit switch) device is in inventory:

| Method | Path |
|--------|------|
| List/create | `/api/ocs/<device_id>/snapshots/` |
| Detail | `/api/ocs/snapshots/<id>/` |
| Download | `…/download/` |
| Restore | `…/restore/` |

Take a snapshot before disruptive rewires. Restore is privileged and audited.
