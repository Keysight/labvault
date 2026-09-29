# `connect/migrations/`

Schema migrations for the `connect` app. The customer SKU starts from a squashed initial
migration; there is no upgrade path from pre-SKU migration histories.

| File | Purpose |
|---|---|
| `0001_customer_initial.py` | Initial schema (45 models) for the customer SKU, generated in one step |
| `0002_alter_labvaultglobalprefs_ui_column_profiles.py` | `LabvaultGlobalPrefs.ui_column_profiles` → `JSONField(default=dict)` with help text |
| `0003_cli_appliance_control.py` | Appliance CLI: adds `reason`, `target`, `result_state`, `result_redacted`, `remote_addr` to `CliInvocation`, the `control_labvault_services` permission, and the `CliAuthThrottle` model (hand-written) |

Two databases: `connect/db_routers.py` (`NPTimeseriesRouter`) routes the time-series models
(`LabMetricSample`, `LabMetricRollup`, `LabResourceEvent`, `PortUsageSample`,
`NPResourceSample`, ...) to `np_timeseries` and restricts their migrations to that alias.
Always migrate both:

```bash
python manage.py migrate --noinput
python manage.py migrate --database np_timeseries --noinput
python manage.py makemigrations --check --dry-run   # CI fails if models drift from migrations
```

The container entrypoint, `labvaultctl install`, and `ensure_topology_insights` all run
these. `labvault_migration_health` checks drift for older migration names and does not apply
to this history.
