# `connect/` — LabVault application

Django app for inventory, device access, Keysight chassis, topology, metrics,
and the staff CLI. Project settings package is [`../labvault/`](../labvault/README.md).

Architecture map: [docs/development/ARCHITECTURE.md](../docs/development/ARCHITECTURE.md).
Data pipelines: [docs/development/DATA_FLOW.md](../docs/development/DATA_FLOW.md).
Where to edit: [docs/development/EXTENDING.md](../docs/development/EXTENDING.md).

## Directory guide

| Path | What lives here | Doc |
|------|-----------------|-----|
| `views.py` | Device pages, dashboard, device cache, OCS page assembly | [core-web](../docs/development/subsystems/core-web.md) |
| `urls.py` | Every HTTP route | [core-web](../docs/development/subsystems/core-web.md) |
| `models.py` | Inventory, chassis, topology, metrics, CLI, audit | [core-web](../docs/development/subsystems/core-web.md) |
| `settings.py` | Django settings (`DJANGO_SETTINGS_MODULE=connect.settings`) | [core-web](../docs/development/subsystems/core-web.md) |
| `drivers/` | Vendor drivers (`get_driver`) | [drivers](../docs/development/subsystems/drivers.md) |
| `keysight_drivers/` | IxOS, KCOS, BPS chassis drivers | [drivers](../docs/development/subsystems/drivers.md) |
| `keysight_*.py` | Chassis UI, discovery, port cache, reservations | [keysight-chassis](../docs/development/subsystems/keysight-chassis.md) |
| `fleet_*.py` | `/api/fleet/*`, heartbeat, OpenAPI | [fleet-api](../docs/development/subsystems/fleet-api.md) |
| `topology*.py`, `lldp_persistence.py` | Discovered LLDP map | [topology](../docs/development/subsystems/topology.md) |
| `lab_topology_*.py` | Lab designer, fabric, import/export | [lab-topology-designer](../docs/development/subsystems/lab-topology-designer.md) |
| `ocs_*.py`, `views_ocs_xconnect.py` | Optical switch panel and patch API | [ocs](../docs/development/subsystems/ocs.md) |
| `lab_metrics.py`, `metric_collectors.py`, `lab_usage_*.py`, `port_usage*.py` | Time series and Lab Pulse | [metrics-insights](../docs/development/subsystems/metrics-insights.md) |
| `changelog.py` | Change-log writers | [metrics-insights](../docs/development/subsystems/metrics-insights.md) |
| `diagnostics*.py`, `log_ring.py` | Health report and support bundle | [diagnostics](../docs/development/subsystems/diagnostics.md) |
| `labvault_cli/` | Staff CLI registry and handlers | [cli](../docs/development/subsystems/cli.md) |
| `management/commands/` | Workers and operator commands | [operations](../docs/development/subsystems/operations.md) |
| `templates/` | HTML | [templates/README.md](templates/README.md) |
| `static/` | CSS and lab-graph JS | [static/README.md](static/README.md) |
| `templatetags/` | Template filters | [templatetags/README.md](templatetags/README.md) |
| `tests/` | Django tests | [tests/README.md](tests/README.md) |
| `migrations/` | Schema history | [migrations/README.md](migrations/README.md) |

## Rules that apply to every module

- Talk to hardware only through a driver or an existing management command.
  Views should prefer the device cache and schedule a refresh.
- Time-series reads and writes use the `np_timeseries` database alias.
- Do not log passwords, API tokens, or the bootstrap password file.
- New HTTP mutations that change lab gear need an auth check and an audit row.
