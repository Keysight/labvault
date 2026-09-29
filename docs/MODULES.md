# Product modules — complete catalog

Every customer-facing surface, with primary URL and doc.

## Core lab

| Module | Primary URLs | Doc |
|--------|--------------|-----|
| Dashboard | `/` `/dashboard/` | [user/INVENTORY.md](user/INVENTORY.md) |
| Devices | device CRUD, health, backups | [user/DEVICES.md](user/DEVICES.md) |
| Keysight / Ixia chassis | `/keysight/*` | [user/KEYSIGHT_CHASSIS.md](user/KEYSIGHT_CHASSIS.md) |
| BMC / node inventory | BMC associations, board, node inventory | [user/KEYSIGHT_CHASSIS.md](user/KEYSIGHT_CHASSIS.md) |
| Reservations | `/keysight/reservations/*` | [user/RESERVATIONS.md](user/RESERVATIONS.md) |
| Topology map | `/topology/` | [user/TOPOLOGY.md](user/TOPOLOGY.md) |
| Lab topology designer | `/lab-topology/*` | [user/TOPOLOGY.md](user/TOPOLOGY.md) |
| Fabric / port fabric | lab-topology fabric pages | [user/FABRIC.md](user/FABRIC.md) |
| Insights / usage graphs | usage, pulse, radar, matrix, timeline | [user/INSIGHTS.md](user/INSIGHTS.md) |
| Reports | fleet / inventory / SLA / change log | [user/REPORTS.md](user/REPORTS.md) |
| Alerts / compliance | monitoring + compliance nav | [user/ALERTS.md](user/ALERTS.md) |
| Audit | `/audit_log/` | [user/AUDIT.md](user/AUDIT.md) |
| Diagnostics | `/diagnostics/` plus export / live / ingest | [admin/TROUBLESHOOTING.md](admin/TROUBLESHOOTING.md) |
| OCS patch snapshots | `/api/ocs/<id>/snapshots/` | [user/FABRIC.md](user/FABRIC.md) |
| Import / export | dataset + bundle commands/UI | [admin/BACKUP_RESTORE.md](admin/BACKUP_RESTORE.md) |
| Settings / tokens / webhooks | `/settings/` | [admin/RUNTIME_SETTINGS.md](admin/RUNTIME_SETTINGS.md) · rotate tokens: [getting-started/FIRST_LOGIN.md](getting-started/FIRST_LOGIN.md) |

## Automation

| Module | Primary URLs | Doc |
|--------|--------------|-----|
| LabVault CLI | `/cli/` `/api/cli/v1/*` | [cli/LABVAULT_CLI.md](cli/LABVAULT_CLI.md) |
| Fleet APIs | `/api/fleet/*` `/api/docs/` | [api/OPENAPI.md](api/OPENAPI.md) |
| opsd service control | Unix socket | [cli/SERVICE_CONTROL.md](cli/SERVICE_CONTROL.md) |
| Workers | heartbeat, collector, refresh, cli-worker | [admin/SERVICES.md](admin/SERVICES.md) |

## Platform

| Module | Doc |
|--------|-----|
| Users / roles / LDAP | [admin/USERS_AND_ROLES.md](admin/USERS_AND_ROLES.md) · [install/LDAP.md](install/LDAP.md) |
| Backup / restore | [admin/BACKUP_RESTORE.md](admin/BACKUP_RESTORE.md) |
| TLS / nginx | [install/TLS.md](install/TLS.md) |
| Hardening | [security/HARDENING.md](security/HARDENING.md) |

## Explicitly removed (404 / absent)

| Surface | Behavior |
|---------|----------|
| Routes outside this tree | 404 JSON |
| docker.sock / log-agent | Not present |
