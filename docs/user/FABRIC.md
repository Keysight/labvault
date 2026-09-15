# Fabric & port fabric

Fabric views hang off a lab topology:

- Fabric map (`/lab-topology/<id>/fabric.json`, `fabric-graph.json`)
- Port fabric (`/lab-topology/<id>/port-fabric.json` and the HTML page)
- Fabric snapshots

`/lab-topology/<id>/port-fabric/summary.json` is a LaaS leftover and returns **404** in this SKU. Use `port-fabric.json` instead.

## OCS patch snapshots

When an OCS (optical circuit switch) device is in inventory:

| Method | Path |
|--------|------|
| List/create | `/api/ocs/<device_id>/snapshots/` |
| Detail | `/api/ocs/snapshots/<id>/` |
| Download | `…/download/` |
| Restore | `…/restore/` |

Take a snapshot before disruptive rewires. Restore is privileged and audited.
