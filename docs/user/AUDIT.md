# Audit

Privileged actions are recorded for accountability.

## Surfaces

| Surface | Path / mechanism |
|---------|------------------|
| UI audit log | `/audit_log/` |
| Access by IP | `/audit_log/access_by_ip/` |
| Keysight operations | Chassis deploy/upgrade/changelog APIs |
| OCS snapshot restore | Audited on restore |
| LabVault CLI mutating commands | `settings set`, service start/stop via opsd |

## Practice

- Prefer named staff accounts over shared `admin` after bootstrap
- Review audit before and after maintenance windows
- Pair audit rows with diagnostics export when escalating incidents

## Related

- [USERS_AND_ROLES.md](../admin/USERS_AND_ROLES.md)
- [SERVICE_CONTROL.md](../cli/SERVICE_CONTROL.md)
- [HARDENING.md](../security/HARDENING.md)
