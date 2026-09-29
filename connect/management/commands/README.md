# `connect/management/commands/`

Django management commands (`python manage.py <name>`). The full table — purpose, typical
invocation, which service runs it, side effects — is in
[docs/development/subsystems/operations.md](../../../docs/development/subsystems/operations.md#management-commands).

Commands that talk to lab gear say so in their module docstring. Prefer `--dry-run` where
offered, and do not run the write-capable ones (`enable_lldp`, `enable_chassis_lldp`,
`sonic_lldp`, `push_config`, `repair_ocs_patches --apply`) against shared gear without the
owner's agreement.

## Long-running workers

| File | Service (systemd / Compose) |
|---|---|
| `run_fleet_heartbeat.py` | `labvault-heartbeat` / `heartbeat` |
| `run_metric_collector.py` | `labvault-collector` / `collector` |
| `run_labvault_refresh.py` | `labvault-refresh` / `refresh` |
| `run_cli_worker.py` | `labvault-cli-worker` / `jobs` |
| `run_cli_ssh.py` | `labvault-cli-ssh` / `cli-ssh` |

## Install, bootstrap, config

| File | Purpose |
|---|---|
| `bootstrap_labvault.py` | First boot: runtime modes, admin account, fleet token, optional dataset restore |
| `ensure_topology_insights.py` | Migrate and verify `np_timeseries`; disable topologies with nothing to collect |
| `validate_external_config.py` | Fail closed on unsafe secret key, hosts, TLS, DB URLs, env file permissions |
| `ensure_godmode_account.py` | Break-glass Django `/admin/` superuser |
| `ensure_shared_admin_account.py` | Shared UI account without `/admin/` access |
| `deprecate_django_admin_user.py` | Remove `/admin/` staff flag from a shared account |
| `labvault_migration_health.py` | Legacy migration-drift report |
| `seed_compliance.py` | Seed compliance rule templates |

## Import / export

| File | Purpose |
|---|---|
| `export_labvault_dataset.py` / `import_labvault_dataset.py` | `labvault-full-export` JSON |
| `export_labvault_bundle.py` / `import_labvault_bundle.py` | `labvault-multibundle` `.tar.gz` |
| `export_diagnostics.py` | Diagnostics JSON or tarball |
| `import_lab_topology.py` | Layout or v3 topology JSON → `LabTopology` |
| `import_ocs_site_config.py` | Site JSON → `Device` / `KeysightChassis` |
| `populate_ocs_lab_topology.py` | Site JSON → new `LabTopology` |
| `validate_ocs_site_config.py` | Enrich site JSON with DB status and live LLDP |

## Metrics and topology maintenance

| File | Purpose |
|---|---|
| `collect_topology_metrics.py` | Manual collector run (one-shot or `--daemon`) |
| `cleanup_np_timeseries.py` | Prune aged time-series rows (daily cron) |
| `rebuild_metric_rollups.py` | Rebuild hourly rollups |
| `benchmark_timeline.py` | Timeline benchmark (dev only; seeds synthetic rows) |
| `changelog_scan.py` | Move / offline / BMC reachability events |
| `refresh_topology_fabric_cache.py` | Pre-warm Port Fabric snapshots |
| `split_hbg_sub_topologies.py` | Split a parent topology into with/without-OCS children |
| `populate_topology_v6.py` | Push topology IPv6 management addresses into inventory |

## LLDP, discovery, device tooling

| File | Purpose |
|---|---|
| `discover_site_topology.py` | Tag-scoped LLDP discovery |
| `lldp_check.py` | Probe, print LLDP, run discovery |
| `refresh_lldp.py` | SSH LLDP fetch from Arista/SONiC switches into the LLDP cache |
| `enable_lldp.py` | Enable LLDP on devices (writes config) |
| `enable_chassis_lldp.py` | Enable IxOS LLDP peer-info (restarts IxServer) |
| `sonic_lldp.py` | Check / enable LLDP on one SONiC switch |
| `push_config.py` | Push config lines to devices |
| `ipmi_discover.py` | Discover Redfish/IPMI BMCs |
| `kcos_inspect_api.py` | Print KCOS REST field names (dev) |
| `validate_app.py` | In-process URL/model/view smoke (dev; writes temporary rows) |

## OCS addressing

| File | Purpose |
|---|---|
| `repair_ocs_patches.py` | Diff live OCS cross-connects vs a schema; `--apply` adds missing ones |
| `apply_ocs_dhcpv6_lab.py` | Mark OCS rows DHCPv6 / prefer IPv6 for display |
| `enable_ocs_lab_dual_stack.py` | Set `preferred_ip_version=dual` on OCS lab rows |
| `validate_ocs_ipv6_mgmt.py` | Check IPv6 management reachability |
| `prefer_ocs_ipv6_mgmt.py` | Switch OCS lab rows to prefer IPv6 after validation |
