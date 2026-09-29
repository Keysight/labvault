# Data flow

End-to-end pipelines. Each box names the function that does the work. Subsystem
pages linked from [ARCHITECTURE.md](ARCHITECTURE.md) have the sequence diagrams.

Addresses below are examples (`192.0.2.10`), not a lab.

## 1. Device page (switch, firewall, or OCS)

```mermaid
sequenceDiagram
  participant Browser
  participant device_detail
  participant Cache as device_data cache
  participant fetch as fetch_device_data
  participant Driver

  Browser->>device_detail: GET /device/<id>/
  device_detail->>Cache: fresh if age < vendor TTL
  alt cache fresh
    Cache-->>Browser: render shelves, LLDP, health
  else stale or ?refresh=1
    device_detail->>fetch: background if stale, inline if empty
    fetch->>Driver: probe + get_system_info + get_interfaces
    Note over Driver: OCS runs restversion, ports, crossconnects in parallel
    fetch->>Cache: _set_cached_data
    fetch-->>Browser: render
  end
```

- Non-OCS: `probe()` then `get_system_info()` then `get_interfaces()` then LLDP.
  Driver choice is `connect.drivers.get_driver(device)` from `vendor_type`.
- OCS: `OcsDriver.fetch_ocs_sources_parallel()` (three REST calls), then
  `views._fetch_ocs_device_data` shapes shelves, patch pairs, and path-verify
  rows via `ocs_helpers`. The page polls `/device/<id>/ocs-patch.json` every
  25 s; `?refresh=1` clears the cache and fetches again.
- A successful cross-connect add/delete (`views_ocs_xconnect`) deletes the
  cache key and schedules a background refresh.
- Credentials stay on the `Device` row. Drivers do not log passwords.

Guide: [ocs.md](subsystems/ocs.md), [drivers.md](subsystems/drivers.md),
[core-web.md](subsystems/core-web.md).

## 2. Keysight chassis and fleet heartbeat

```mermaid
flowchart LR
  DISC["keysight_discovery / add form"] --> ROW["KeysightChassis row"]
  REF["run_labvault_refresh ~120s"] --> FETCH["fetch_chassis_data"]
  FETCH --> DRV["IxOS / KCOS / BPS driver"]
  FETCH --> CC["keysight:chassis_data:id"]
  HB["run_fleet_heartbeat"] --> STORE["fleet_heartbeat:v1"]
  HB --> CC
  CC --> UI["dashboard, chassis detail, card grid"]
  STORE --> API["/api/fleet/*"]
  ROW --> API
```

Card grids on the detail page are built from cached `/ports` telemetry when a
full fetch has not finished. The detail page may start one background full
fetch per chassis. Fleet clients authenticate with a Bearer `APIToken` or a
logged-in session.

Guide: [keysight-chassis.md](subsystems/keysight-chassis.md),
[fleet-api.md](subsystems/fleet-api.md).

## 3. Discovered topology vs planned lab topology

Two models, easy to confuse:

| | Discovered map `/topology/` | Lab designer `/lab-topology/` |
|--|-----------------------------|-------------------------------|
| Source of truth | LLDP from devices and chassis | Operator-drawn nodes and links |
| Tables | `TopologyLink`, `ChassisDeviceLink` | `LabTopology`, `LabTopologyNode`, `LabTopologyLink` |
| Refresh | `lldp_check`, `refresh_lldp`, rescan button | Import, save, `refresh_topology_fabric_cache` |
| Live overlay | neighbor match by hostname / IP / MAC | fabric view merges plan + live OCS + LLDP + DAC serials |

LLDP neighbors are also written to a 24-hour JSON store
(`lldp_persistence`) so a graph can still be drawn when a device is briefly
unreachable.

Guide: [topology.md](subsystems/topology.md),
[lab-topology-designer.md](subsystems/lab-topology-designer.md).

## 4. Metrics and Lab Pulse

```mermaid
flowchart LR
  COL["run_metric_collector 60s"] --> MC["collect_all_topologies"]
  MC --> CH["chassis REST counters"]
  MC --> SW["Arista / SONiC counters"]
  MC --> OCS["OCS crossconnect diff events"]
  CH --> TS[("np_timeseries")]
  SW --> TS
  OCS --> TS
  TS --> BUCK["get_metric_buckets"]
  TS --> USAGE["port_usage 31-day clip"]
  BUCK --> PULSE["Lab Pulse JSON"]
  USAGE --> PULSE
  PULSE --> JS["lab-graph/*.js"]
```

`collector_mode` (runtime setting) defaults to idle on this SKU, so the
collector does not poll hardware until an operator sets it to live.
`CollectorState` keeps byte-counter baselines in memory; only those baselines
survive a process restart. Insights snapshots are refreshed at the end of
each collector tick.

Guide: [metrics-insights.md](subsystems/metrics-insights.md).

## 5. Staff CLI and service control

```mermaid
sequenceDiagram
  participant User
  participant Front as /cli/ or SSH :2222 or POST /api/cli/v1/invoke/
  participant Reg as labvault_cli registry
  participant DB as CliInvocation
  participant Ops as opsd socket

  User->>Front: verb + args
  Front->>Reg: parse, tier check, confirm nonce if needed
  Reg->>DB: audit row (redacted)
  alt service start/stop/restart
    Reg->>Ops: JSON over Unix socket
    Ops-->>Reg: unit result
  else data command
    Reg->>DB: query or mutate allowlisted models
  end
  Reg-->>User: redacted text
```

There is no OS shell and no free-form device shell. Service control is the
only path that starts or stops processes, and it goes through opsd.

Guide: [cli.md](subsystems/cli.md), [operations.md](subsystems/operations.md).

## 6. Import and export

| Format | Writer | Reader | Contains |
|--------|--------|--------|----------|
| `labvault-full-export` JSON | `export_labvault_dataset` / UI | `import_labvault_dataset` | Inventory, topologies, runtime modes |
| Multibundle `.tar.gz` | `export_labvault_bundle` | `import_labvault_bundle` | Inventory, topologies, site files, optional SQLite and secrets |
| Diagnostics tarball | `export_diagnostics --bundle` | support hand-off | Health JSON, optional logs |
| Site JSON | external or `import_ocs_site_config` | OCS helpers, topology populate | OCS triplets, switch ports, chassis |

Secrets are omitted unless the caller passes the include-secrets flag.
Guide: [operations.md](subsystems/operations.md).

## 7. First boot

Installer (`oneshot-systemd.sh`, Compose entry, or air-gap script) runs
migrations, then `bootstrap_labvault`. That command creates the admin user,
writes the initial password to a root-only file (not into the docs or the
database in clear text for later reads), creates the fleet API token file,
and applies runtime-setting defaults. `validate_external_config` runs before
gunicorn binds. Login steps for an operator are
[FIRST_LOGIN.md](../getting-started/FIRST_LOGIN.md).
