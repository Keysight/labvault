# Architecture

- Django project `labvault` + app `connect`
- Dual database: `default` + `np_timeseries`
- Workers as management commands (systemd or compose)
- LabVault CLI jobs: `CliJob` / `CliInvocation`
- opsd: separate process, Unix socket JSON protocol
- Fleet APIs: Bearer automation surface
- Customer flags: `LABVAULT_CUSTOMER_SKU` hard-dumps excluded products

See [MODULES.md](../MODULES.md).
