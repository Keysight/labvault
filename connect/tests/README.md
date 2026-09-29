# `connect/tests/`

Django test modules for the `connect` app. None need lab hardware, root, systemd, or
Docker: drivers, SSH, and HTTP calls are mocked, and opsd is loaded by path and driven
through `handle()`.

```bash
export DJANGO_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
export DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,testserver
python manage.py test connect.tests --verbosity=1                 # everything
python manage.py test connect.tests.test_labvault_cli_appliance   # one module
bash tools/run_release_gates.sh                                   # CI subset + gates
```

The CI subset (`ci.yml`, `tools/run_release_gates.sh`) is: `test_hard_dump`,
`test_bootstrap_defaults`, `test_csrf_exempt_inventory`, `test_labvault_cli_catalog`,
`test_redact_payload`, `test_validate_external_config`, `test_device_detail_render`,
`test_tls_settings`, `test_diagnostics`, `test_cli_ssh_auth`, `test_labvault_cli_commands`,
`test_health_endpoints`.

| Group | Modules |
|---|---|
| SKU guards | `test_hard_dump` (excluded routes/modules absent), `test_csrf_exempt_inventory`, `test_labvault_flags`, `test_database_url_settings`, `test_tls_settings`, `test_validate_external_config` |
| Bootstrap and accounts | `test_bootstrap_defaults`, `test_first_login_rotation`, `test_django_admin_access`, `test_password_policy` |
| CLI and opsd | `test_labvault_cli_appliance`, `test_labvault_cli_catalog`, `test_labvault_cli_commands`, `test_cli_ssh_auth`, `test_redact_payload`, `test_opsd_broker` |
| Health, diagnostics, fleet | `test_health_endpoints`, `test_diagnostics`, `test_fleet_api_cache`, `test_fleet_heartbeat_deploy`, `test_fleet_heartbeat_live` |
| Import / export | `test_dataset_import_enables_pulse`, `test_labvault_bundle`, `test_lab_topology_io`, `test_topology_export`, `test_lab_site_compiler` |
| Metrics and Insights | `test_run_metric_collector`, `test_collectors_enhanced`, `test_metric_aggregation`, `test_lab_timeline`, `test_timeline_api`, `test_port_usage`, `test_usage_insights`, `test_usage_graph_views`, `test_usage_page_theme`, `test_topology_metrics_collection_toggle`, `test_event_grouping`, `test_change_log_report` |
| Topology and fabric | `test_topology_graph`, `test_topology_api`, `test_topology_fabric_cache`, `test_topology_lldp_resolve`, `test_topology_dac_finder`, `test_fabric_lldp_merge`, `test_hbg_fabric_connectivity`, `test_lab_topology_enrich`, `test_lab_topology_split`, `test_hardware_links` |
| Drivers and addressing | `test_arista_driver`, `test_aresone_fanout`, `test_aresone_ssh`, `test_driver_registry`, `test_ip_addressing`, `test_ixos_lldp_peer`, `test_kcos_chassis_api`, `test_kcos_lldp`, `test_keysight_aps_generations`, `test_keysight_cards_from_ports`, `test_keysight_hw_errors`, `test_keysight_slack`, `test_ocs_driver_parallel`, `test_pcpu_versions` |
| UI render | `test_device_detail_render` |

Support files:

| File | Purpose |
|---|---|
| `timeline_test_helpers.py` | `create_topology()` and sample builders shared by timeline tests and the `benchmark_timeline` command |
| `fixtures/topology_graph_minimal.json` | Minimal topology graph fixture |

`tools/check_public_source.py` skips this directory, so fixtures may use literal lab-like
values; keep them to RFC 5737 addresses anyway.
