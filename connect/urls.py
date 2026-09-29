"""App URLconf, included at ``/`` by ``labvault/urls.py``.

Routes are grouped by area and point into several view modules: ``views`` (devices,
alerts, compliance, config, reports, settings, export/import, audit, legacy REST, OCS
snapshots), ``health_views``, ``labvault_cli_views``, ``lab_topology_views`` /
``lab_topology_onboard`` / ``lab_timeline_views`` / ``lab_usage_*_views`` (Lab Topology
Designer and usage graphs), ``views_ocs_xconnect``, ``diagnostics_views``,
``fleet_api_views`` (Bearer automation API) and ``keysight_views`` /
``keysight_slack_views`` (Keysight chassis). ``api/nexus/*``, ``api/research/*`` and
``api/hw-assignments/release`` resolve to stubs that always return 404 JSON.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path
from . import views
from . import health_views
from . import labvault_cli_views
from . import keysight_views
from . import keysight_slack_views
from . import lab_topology_views
from . import lab_timeline_views
from . import lab_usage_insights_views
from . import lab_usage_graph_views
from . import fleet_api_views
from . import views_ocs_xconnect
from . import lab_topology_onboard
from . import diagnostics_views

urlpatterns = [
    path('health/live', health_views.health_live, name='health_live'),
    path('health/ready', health_views.health_ready, name='health_ready'),
    path('cli/', labvault_cli_views.cli_page, name='labvault_cli'),
    path('api/cli/v1/commands/', labvault_cli_views.cli_commands, name='cli_commands'),
    path('api/cli/v1/confirm/', labvault_cli_views.cli_confirm, name='cli_confirm'),
    path('api/cli/v1/invoke/', labvault_cli_views.cli_invoke, name='cli_invoke'),
    path('api/cli/v1/jobs/<int:job_id>/', labvault_cli_views.cli_job, name='cli_job'),
    path('api/cli/v1/history/', labvault_cli_views.cli_history, name='cli_history'),

    # Authentication
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Dashboard
    path('', views.dashboard, name='home'),
    path('dashboard/', views.dashboard, name='dashboard'),

    # Device management
    path('device/<int:device_id>/', views.device_detail, name='device_detail'),
    path('device/<int:device_id>/health/', views.device_health, name='device_health'),
    path('device/<int:device_id>/health/history/', views.device_health_history, name='device_health_history'),
    path('device/<int:device_id>/routing/', views.device_routing, name='device_routing'),
    path('device/<int:device_id>/vlans/', views.device_vlans, name='device_vlans'),
    path('device/<int:device_id>/config/', views.device_config, name='device_config'),
    path('device/<int:device_id>/config/backup/', views.backup_config, name='backup_config'),
    path('device/<int:device_id>/config/timeline/', views.config_timeline, name='config_timeline'),
    path('device/<int:device_id>/arp/', views.device_arp, name='device_arp'),
    path('device/<int:device_id>/mac/', views.device_mac, name='device_mac'),
    path('device/<int:device_id>/lldp/', views.device_lldp, name='device_lldp'),
    path('device/<int:device_id>/enable-lldp/', views.device_enable_lldp, name='device_enable_lldp'),
    path('device/<int:device_id>/policies/', views.device_policies, name='device_policies'),
    path('device/<int:device_id>/vpn/', views.device_vpn, name='device_vpn'),
    path('device/<int:device_id>/quick-status/', views.device_quick_status, name='device_quick_status'),

    # Enhanced device pages
    path('device/<int:device_id>/bgp/', views.device_bgp, name='device_bgp'),
    path('device/<int:device_id>/ospf/', views.device_ospf, name='device_ospf'),
    path('device/<int:device_id>/environment/', views.device_environment, name='device_environment'),
    path('device/<int:device_id>/dom/', views.device_dom, name='device_dom'),

    # AJAX endpoints for device data
    path('device/<int:device_id>/bgp/json/', views.device_bgp_json, name='device_bgp_json'),
    path('device/<int:device_id>/ospf/json/', views.device_ospf_json, name='device_ospf_json'),
    path('device/<int:device_id>/environment/json/', views.device_environment_json, name='device_environment_json'),
    path('device/<int:device_id>/counters/json/', views.device_counters_json, name='device_counters_json'),
    path('device/<int:device_id>/interface-trending/', views.device_interface_trending, name='device_interface_trending'),
    path('device/<int:device_id>/ocs-patch.json', views.api_device_ocs_patch, name='api_device_ocs_patch'),

    # Device CRUD
    path('add_device/', views.add_device, name='add_device'),
    path('edit_device/<int:device_id>/', views.edit_device, name='edit_device'),
    path('delete_device/<int:device_id>/', views.delete_device, name='delete_device'),

    # Bulk operations
    path('bulk_action/', views.bulk_action, name='bulk_action'),

    # Alerts
    path('alerts/', views.alerts_view, name='alerts'),
    path('alerts/<int:alert_id>/ack/', views.acknowledge_alert, name='acknowledge_alert'),
    path('alerts/ack-all/', views.acknowledge_all_alerts, name='acknowledge_all_alerts'),

    # Compliance
    path('compliance/', views.compliance_view, name='compliance'),
    path('compliance/run/', views.run_compliance_check, name='run_compliance_check'),

    # Config management
    path('config-search/', views.config_search, name='config_search'),
    path('config/diff/<int:backup_id>/', views.config_diff_view, name='config_diff_view'),

    # Saved commands
    path('command/save/', views.save_command, name='save_command'),
    path('command/<int:cmd_id>/delete/', views.delete_saved_command, name='delete_saved_command'),

    # Topology
    path('topology/', views.topology_map, name='topology'),
    path('topology/data/', views.topology_data, name='topology_data'),
    path('topology/refresh/', views.topology_refresh, name='topology_refresh'),
    path('topology/rescan/', views.topology_rescan, name='topology_rescan'),
    path('topology/export/', views.topology_export, name='topology_export'),
    # Lab Topology Designer
    path('lab-topology/', lab_topology_views.lab_topology_list, name='lab_topology_list'),
    path('lab-topology/new/', lab_topology_views.lab_topology_new, name='lab_topology_new'),
    path('lab-topology/onboard/', lab_topology_onboard.lab_topology_onboard, name='lab_topology_onboard'),
    path('lab-topology/onboard/preview/', lab_topology_onboard.lab_topology_onboard_preview, name='lab_topology_onboard_preview'),
    path('lab-topology/onboard/build/', lab_topology_onboard.lab_topology_onboard_build, name='lab_topology_onboard_build'),
    path('lab-topology/build-inventory/', lab_topology_views.lab_topology_build_inventory, name='lab_topology_build_inventory'),
    path('lab-topology/build-dc/', lab_topology_views.lab_topology_build_dc, name='lab_topology_build_dc'),
    path('lab-topology/from-lldp/', lab_topology_views.lab_topology_from_lldp, name='lab_topology_from_lldp'),
    path('lab-topology/<int:topo_id>/discover-dac/', lab_topology_views.lab_topology_discover_dac, name='lab_topology_discover_dac'),
    path('lab-topology/<int:topo_id>/nodes/<int:node_id>/ports/', lab_topology_views.lab_topology_node_ports, name='lab_topology_node_ports'),
    path('lab-topology/<int:topo_id>/validate-link/', lab_topology_views.lab_topology_validate_link, name='lab_topology_validate_link'),
    path('lab-topology/<int:topo_id>/', lab_topology_views.lab_topology_detail, name='lab_topology_detail'),
    path('lab-topology/<int:topo_id>/data/', lab_topology_views.lab_topology_data, name='lab_topology_data'),
    path(
        'lab-topology/<int:topo_id>/metrics-collection/',
        lab_topology_views.lab_topology_metrics_collection,
        name='lab_topology_metrics_collection',
    ),
    path('lab-topology/<int:topo_id>/clone/', lab_topology_views.lab_topology_clone, name='lab_topology_clone'),
    path(
        'lab-topology/<int:topo_id>/split-subtopologies/',
        lab_topology_views.lab_topology_split_subtopologies,
        name='lab_topology_split_subtopologies',
    ),
    path('lab-topology/<int:topo_id>/export/', lab_topology_views.lab_topology_export, name='lab_topology_export'),
    path('lab-topology/<int:topo_id>/import/', lab_topology_views.lab_topology_import, name='lab_topology_import'),
    path('lab-topology/<int:topo_id>/inventory/', lab_topology_views.lab_topology_inventory, name='lab_topology_inventory'),
    path('lab-topology/<int:topo_id>/nodes/', lab_topology_views.lab_topology_node_add, name='lab_topology_node_add'),
    path('lab-topology/<int:topo_id>/nodes/<int:node_id>/', lab_topology_views.lab_topology_node_detail, name='lab_topology_node_detail'),
    path('lab-topology/<int:topo_id>/links/', lab_topology_views.lab_topology_link_add, name='lab_topology_link_add'),
    path('lab-topology/<int:topo_id>/links/<int:link_id>/', lab_topology_views.lab_topology_link_delete, name='lab_topology_link_delete'),
    path('lab-topology/<int:topo_id>/push-port-config/', lab_topology_views.lab_topology_push_port_config, name='lab_topology_push_port_config'),
    # Site-topology import (two-file upload)
    path('lab-topology/import-site/', lab_topology_views.lab_topology_import_site, name='lab_topology_import_site'),
    # Enhanced data endpoint (includes link.extra)
    path('lab-topology/<int:topo_id>/data/v2/', lab_topology_views.lab_topology_data_v2, name='lab_topology_data_v2'),
    # Test Setup Builder
    path('lab-topology/<int:topo_id>/test-setup/', lab_topology_views.test_setup_list, name='test_setup_list'),
    path('lab-topology/<int:topo_id>/test-setup/calculate/', lab_topology_views.test_setup_calculate, name='test_setup_calculate'),
    path('test-setup/<int:template_id>/', lab_topology_views.test_setup_detail, name='test_setup_detail'),
    path('test-setup/<int:template_id>/apply/', lab_topology_views.test_setup_apply, name='test_setup_apply'),
    path('test-setup/run/<int:run_id>/status/', lab_topology_views.test_setup_run_status, name='test_setup_run_status'),
    # Scenario analysis (current vs needed OCS patches)
    path('lab-topology/<int:topo_id>/scenario/', lab_topology_views.lab_topology_scenario, name='lab_topology_scenario'),
    path('lab-topology/<int:topo_id>/portmap/', lab_topology_views.lab_topology_portmap, name='lab_topology_portmap'),
    # Delete topology
    path('lab-topology/<int:topo_id>/delete/', lab_topology_views.lab_topology_delete, name='lab_topology_delete'),
    # Fabric Map (device-level draw.io view)
    path('lab-topology/by-name/<path:name>/', lab_topology_views.lab_topology_by_name, name='lab_topology_by_name'),
    path('lab-topology/list.json', lab_topology_views.lab_topology_list_json, name='lab_topology_list_json'),
    path('lab-topology/<int:topo_id>/port-fabric/summary.json', lab_topology_views.lab_port_fabric_summary_api, name='lab_port_fabric_summary_api'),
    path('lab-topology/<int:topo_id>/ports/available/', lab_topology_views.lab_topology_ports_available, name='lab_topology_ports_available'),
    path('lab-topology/<int:topo_id>/graph.json', lab_topology_views.lab_topology_graph_json, name='lab_topology_graph_json'),
    path('lab-topology/<int:topo_id>/graph/compare.json', lab_topology_views.lab_topology_graph_compare, name='lab_topology_graph_compare'),
    path('lab-topology/<int:topo_id>/graph/refresh/', lab_topology_views.lab_topology_graph_refresh, name='lab_topology_graph_refresh'),
    path('lab-topology/<int:topo_id>/graph/validate/', lab_topology_views.lab_topology_graph_validate, name='lab_topology_graph_validate'),
    path('lab-topology/<int:topo_id>/fabric/', lab_topology_views.lab_fabric_map_page, name='lab_fabric_map'),
    path('lab-topology/<int:topo_id>/fabric.json', lab_topology_views.lab_fabric_map_api, name='lab_fabric_map_api'),
    # Port Fabric Map (port-level full DC view)
    path('lab-topology/<int:topo_id>/port-fabric/', lab_topology_views.lab_port_fabric_page, name='lab_port_fabric'),
    path('lab-topology/<int:topo_id>/port-fabric.json', lab_topology_views.lab_port_fabric_api, name='lab_port_fabric_api'),
    path('lab-topology/by-name/<path:name>/usage/', lab_topology_views.lab_topology_usage_by_name, name='lab_topology_usage_by_name'),
    path('lab-topology/<int:topo_id>/usage/', lab_topology_views.lab_topology_usage_page, name='lab_topology_usage'),
    path('lab-topology/<int:topo_id>/usage/insights/', lab_usage_insights_views.lab_topology_usage_insights_page, name='lab_topology_usage_insights'),
    path('lab-topology/<int:topo_id>/usage/pulse-radial/', lab_usage_graph_views.lab_topology_graph_pulse_radial, name='lab_topology_graph_pulse_radial'),
    path('lab-topology/<int:topo_id>/usage/stream-wave/', lab_usage_graph_views.lab_topology_graph_stream_wave, name='lab_topology_graph_stream_wave'),
    path('lab-topology/<int:topo_id>/usage/radar-health/', lab_usage_graph_views.lab_topology_graph_radar_health, name='lab_topology_graph_radar_health'),
    path('lab-topology/<int:topo_id>/usage/matrix-heat/', lab_usage_graph_views.lab_topology_graph_matrix_heat, name='lab_topology_graph_matrix_heat'),
    path('lab-topology/<int:topo_id>/usage/topology-pulse/', lab_usage_graph_views.lab_topology_graph_topology_pulse, name='lab_topology_graph_topology_pulse'),
    path('lab-topology/<int:topo_id>/usage-graph.json', lab_topology_views.lab_topology_usage_graph_json, name='lab_topology_usage_graph_json'),
    path('lab-topology/<int:topo_id>/usage-insights.json', lab_usage_insights_views.lab_topology_usage_insights_json, name='lab_topology_usage_insights_json'),
    path('lab-topology/<int:topo_id>/timeline-graph.json', lab_timeline_views.lab_topology_timeline_graph_json, name='lab_topology_timeline_graph_json'),
    path('lab-topology/<int:topo_id>/fabric-graph.json', lab_timeline_views.lab_topology_fabric_graph_json, name='lab_topology_fabric_graph_json'),
    path('api/port-usage/episodes/', lab_topology_views.api_port_usage_episodes, name='api_port_usage_episodes'),

    # Device comparison
    path('compare/', views.device_compare, name='device_compare'),

    # Reports
    path('reports/', views.fleet_report, name='fleet_report'),
    path('reports/inventory/', views.inventory_report, name='inventory_report'),
    path('reports/sla/', views.sla_report, name='sla_report'),
    path('reports/changes/', views.change_log_report, name='change_log_report'),

    # Settings
    path('settings/', views.settings_view, name='settings'),
    path('about/', views.about_view, name='about'),
    path('settings/webhook/create/', views.create_webhook, name='create_webhook'),
    path('settings/webhook/<int:webhook_id>/delete/', views.delete_webhook, name='delete_webhook'),
    path('settings/token/create/', views.create_api_token, name='create_api_token'),
    path('settings/token/<int:token_id>/revoke/', views.revoke_api_token, name='revoke_api_token'),
    path('settings/maintenance/create/', views.create_maintenance_window, name='create_maintenance_window'),
    path('settings/job/create/', views.create_scheduled_job, name='create_scheduled_job'),
    path('settings/group/create/', views.create_device_group, name='create_device_group'),

    # Import/Export
    path('export_devices/', views.export_devices, name='export_devices'),
    path('export/labvault/', views.export_labvault_dataset, name='export_labvault_dataset'),
    path('export/labvault/bundle/', views.export_labvault_bundle, name='export_labvault_bundle'),
    path('import_devices/', views.import_devices, name='import_devices'),

    # Audit log
    path('audit_log/', views.audit_log, name='audit_log'),
    path('audit_log/access_by_ip/', views.access_by_ip, name='access_by_ip'),

    # Live status (AJAX)
    path('api/live-status/', views.device_live_status, name='device_live_status'),

    # REST API
    path('api/devices/', views.api_devices, name='api_devices'),
    path('api/device/<int:device_id>/', views.api_device_detail, name='api_device_detail'),
    path('api/device/<int:device_id>/health/', views.api_device_health, name='api_device_health'),
    path('api/alerts/', views.api_alerts, name='api_alerts'),
    path('api/topology/', views.api_topology, name='api_topology'),
    path('api/hw-assignments/release', views.api_hw_assignments_release, name='api_hw_assignments_release'),
    path('api/device-ocs-mapping/<int:device_id>/', views.api_device_ocs_mapping, name='api_device_ocs_mapping'),
    # OCS Patch Snapshots (backup / restore)
    path('api/ocs/<int:device_id>/snapshots/', views.api_ocs_snapshot_create, name='api_ocs_snapshot_create'),
    path('api/ocs/snapshots/<int:snapshot_id>/', views.api_ocs_snapshot_detail, name='api_ocs_snapshot_detail'),
    path('api/ocs/snapshots/<int:snapshot_id>/download/', views.api_ocs_snapshot_download, name='api_ocs_snapshot_download'),
    path('api/ocs/snapshots/<int:snapshot_id>/restore/', views.api_ocs_snapshot_restore, name='api_ocs_snapshot_restore'),
    path('api/ocs/<str:device_ip>/crossconnects/', views_ocs_xconnect.api_ocs_crossconnects_list, name='api_ocs_crossconnects_list'),
    path('api/ocs/<str:device_ip>/crossconnect/', views_ocs_xconnect.api_ocs_crossconnect_mutate, name='api_ocs_crossconnect_mutate'),
    path('diagnostics/', diagnostics_views.diagnostics_center, name='diagnostics_center'),
    path('diagnostics/export/', diagnostics_views.diagnostics_export_page, name='diagnostics_export'),
    path('diagnostics/bundle/', diagnostics_views.diagnostics_bundle_export, name='diagnostics_bundle_export'),
    path('diagnostics/live.json', diagnostics_views.diagnostics_json_live, name='diagnostics_json_live'),
    path('api/diagnostics/ingest/', diagnostics_views.diagnostics_log_ingest, name='diagnostics_log_ingest'),
    path('api/diagnostics.json', diagnostics_views.diagnostics_export_api, name='diagnostics_export_api'),
    path('api/keysight/resources/', keysight_views.api_keysight_resources, name='api_keysight_resources'),

    # ============================================================
    # FLEET APIs (Bearer or session)
    # ============================================================
    path('api/fleet/', fleet_api_views.fleet_index, name='fleet_index'),
    path('api/fleet/openapi.json', fleet_api_views.fleet_openapi, name='fleet_openapi'),
    path('api/docs/', fleet_api_views.fleet_swagger_ui, name='fleet_swagger_ui'),
    path('api/docs', fleet_api_views.fleet_swagger_ui, name='fleet_swagger_ui_noslash'),
    path('api/fleet/health.json', fleet_api_views.fleet_health, name='fleet_health'),
    path('api/fleet/heartbeat.json', fleet_api_views.fleet_heartbeat, name='fleet_heartbeat'),
    path('api/fleet/heartbeat/stream', fleet_api_views.fleet_heartbeat_stream, name='fleet_heartbeat_stream'),
    path('api/fleet/chassis/<int:chassis_id>/health.json', fleet_api_views.fleet_chassis_health, name='fleet_chassis_health'),
    path('api/fleet/chassis/<int:chassis_id>/recover', fleet_api_views.fleet_chassis_recover, name='fleet_chassis_recover'),
    path('api/fleet/chassis/<int:chassis_id>/ports.json', fleet_api_views.fleet_chassis_ports, name='fleet_chassis_ports'),
    path('api/fleet/chassis/<int:chassis_id>/team-tags', fleet_api_views.fleet_team_tags, name='fleet_team_tags'),
    path('api/fleet/ports/telemetry.json', fleet_api_views.fleet_ports_telemetry, name='fleet_ports_telemetry'),
    path('api/fleet/ports/preflight.json', fleet_api_views.fleet_ports_preflight, name='fleet_ports_preflight'),
    path('api/fleet/sla.json', fleet_api_views.fleet_sla, name='fleet_sla'),
    path('api/fleet/summary.json', fleet_api_views.fleet_summary, name='fleet_summary'),
    path('api/fleet/metrics/transmission.json', fleet_api_views.fleet_transmission, name='fleet_transmission'),
    path('api/fleet/inventory.json', fleet_api_views.fleet_inventory, name='fleet_inventory'),
    path('api/fleet/inventory.csv', fleet_api_views.fleet_inventory_csv, name='fleet_inventory_csv'),
    path('api/fleet/reservations.json', fleet_api_views.fleet_reservations, name='fleet_reservations'),
    path('api/fleet/reservations', fleet_api_views.fleet_reservations_create, name='fleet_reservations_create'),
    path('api/fleet/ownership.json', fleet_api_views.fleet_ownership, name='fleet_ownership'),
    path('api/fleet/conflicts.json', fleet_api_views.fleet_conflicts, name='fleet_conflicts'),

    # ============================================================
    # KEYSIGHT / IXIA  (/keysight/ prefix)
    # ============================================================
    path('keysight/', keysight_views.keysight_dashboard, name='keysight_dashboard'),
    path(
        'keysight/refresh-node-slots/',
        keysight_views.keysight_refresh_node_slots,
        name='keysight_refresh_node_slots',
    ),
    path('keysight/add/', keysight_views.keysight_add_chassis, name='keysight_add_chassis'),
    path('keysight/chassis/<int:chassis_id>/', keysight_views.keysight_chassis_detail, name='keysight_chassis_detail'),
    path('keysight/chassis/<int:chassis_id>/edit/', keysight_views.keysight_edit_chassis, name='keysight_edit_chassis'),
    path('keysight/chassis/<int:chassis_id>/delete/', keysight_views.keysight_delete_chassis, name='keysight_delete_chassis'),
    path('keysight/chassis/<int:chassis_id>/sensors/', keysight_views.keysight_chassis_sensors, name='keysight_chassis_sensors'),
    path('keysight/chassis/<int:chassis_id>/licenses/', keysight_views.keysight_chassis_licenses, name='keysight_chassis_licenses'),

    # Keysight AJAX data
    path('keysight/api/chassis/<int:chassis_id>/data/', keysight_views.keysight_chassis_data_json, name='keysight_chassis_data_json'),
    path(
        'keysight/api/chassis/<int:chassis_id>/np-timeseries/',
        keysight_views.keysight_np_timeseries_json,
        name='keysight_np_timeseries_json',
    ),
    path('keysight/api/dashboard/', keysight_views.keysight_dashboard_data_json, name='keysight_dashboard_data_json'),
    path('keysight/api/chassis/<int:chassis_id>/team-tags/', keysight_views.keysight_update_team_tags, name='keysight_update_team_tags'),
    path('keysight/api/chassis/<int:chassis_id>/node-hardware-error/', keysight_views.keysight_set_node_hardware_error, name='keysight_set_node_hardware_error'),

    # Slack bot (slash commands)
    path('api/slack/keysight/', keysight_slack_views.keysight_slack_command, name='keysight_slack_command'),

    # Keysight port/card operations
    path('keysight/api/chassis/<int:chassis_id>/port-operation/', keysight_views.keysight_port_operation, name='keysight_port_operation'),
    path('keysight/api/chassis/<int:chassis_id>/card-operation/', keysight_views.keysight_card_operation, name='keysight_card_operation'),

    # Keysight KCOS operations
    path('keysight/api/chassis/<int:chassis_id>/kcos/switch-app/', keysight_views.keysight_kcos_switch_app, name='keysight_kcos_switch_app'),
    path('keysight/api/chassis/<int:chassis_id>/kcos/power-cycle-node/', keysight_views.keysight_kcos_power_cycle_node, name='keysight_kcos_power_cycle_node'),
    path('keysight/api/chassis/<int:chassis_id>/kcos/restart-node/', keysight_views.keysight_kcos_restart_node, name='keysight_kcos_restart_node'),
    path('keysight/api/chassis/<int:chassis_id>/kcos/power-node/', keysight_views.keysight_kcos_power_node, name='keysight_kcos_power_node'),
    path('keysight/api/chassis/<int:chassis_id>/kcos/reboot-chassis/', keysight_views.keysight_kcos_reboot_chassis, name='keysight_kcos_reboot_chassis'),
    path('keysight/api/chassis/<int:chassis_id>/clear-cache/', keysight_views.keysight_clear_cache, name='keysight_clear_cache'),

    path('keysight/api/bulk-redetect/', keysight_views.keysight_bulk_redetect, name='keysight_bulk_redetect'),

    # Keysight subnet discovery
    path('keysight/discover/', keysight_views.keysight_discover_page, name='keysight_discover'),
    path('keysight/discover/scan/', keysight_views.keysight_discover_scan, name='keysight_discover_scan'),
    path('keysight/discover/scan/<str:scan_id>/status/', keysight_views.keysight_discover_scan_status, name='keysight_discover_scan_status'),
    path('keysight/discover/add-devices/', keysight_views.keysight_discover_add_devices, name='keysight_discover_add_devices'),
    path('keysight/discover/configure/', keysight_views.keysight_discover_configure, name='keysight_discover_configure'),
    path('keysight/discover/<int:scan_id>/delete/', keysight_views.keysight_discover_delete, name='keysight_discover_delete'),

    # Keysight hardware inventory
    path('keysight/inventory/', keysight_views.keysight_hardware_inventory, name='keysight_hardware_inventory'),

    # Keysight snapshot/upgrade operations
    path('keysight/api/chassis/<int:chassis_id>/snapshots/', keysight_views.keysight_snapshots_list, name='keysight_snapshots_list'),
    path('keysight/api/chassis/<int:chassis_id>/snapshot/create/', keysight_views.keysight_snapshot_create, name='keysight_snapshot_create'),
    path('keysight/api/chassis/<int:chassis_id>/snapshot/restore/', keysight_views.keysight_snapshot_restore, name='keysight_snapshot_restore'),
    path('keysight/api/chassis/<int:chassis_id>/snapshot/delete/', keysight_views.keysight_snapshot_delete, name='keysight_snapshot_delete'),
    path('keysight/api/chassis/<int:chassis_id>/upgrade/', keysight_views.keysight_upgrade, name='keysight_upgrade'),
    path('keysight/api/chassis/<int:chassis_id>/upgrade/status/', keysight_views.keysight_upgrade_status, name='keysight_upgrade_status'),

    # Keysight reservations
    path('keysight/reservations/', keysight_views.keysight_reservations, name='keysight_reservations'),
    path('keysight/reservations/create/', keysight_views.keysight_create_reservation, name='keysight_create_reservation'),
    path('keysight/reservations/<int:reservation_id>/', keysight_views.keysight_reservation_detail, name='keysight_reservation_detail'),
    path('keysight/reservations/<int:reservation_id>/edit/', keysight_views.keysight_edit_reservation, name='keysight_edit_reservation'),
    path('keysight/reservations/<int:reservation_id>/cancel/', keysight_views.keysight_cancel_reservation, name='keysight_cancel_reservation'),
    path('keysight/reservations/<int:reservation_id>/delete/', keysight_views.keysight_delete_reservation, name='keysight_delete_reservation'),
    path('keysight/quick-reserve/', keysight_views.keysight_quick_reserve, name='keysight_quick_reserve'),

    # Keysight audit
    path('keysight/audit/', keysight_views.keysight_audit_log, name='keysight_audit_log'),

    # Keysight deployment / upgrade console
    path('keysight/deploy/', keysight_views.keysight_deploy_page, name='keysight_deploy_page'),
    path('keysight/api/deploy/start/', keysight_views.keysight_deploy_start, name='keysight_deploy_start'),
    path('keysight/api/deploy/upload/', keysight_views.keysight_deploy_upload, name='keysight_deploy_upload'),
    path('keysight/api/deploy/packages/', keysight_views.keysight_deploy_list_packages, name='keysight_deploy_list_packages'),
    path('keysight/api/deploy/status/', keysight_views.keysight_deploy_status, name='keysight_deploy_status'),
    path('keysight/node-inventory/', keysight_views.keysight_node_inventory, name='keysight_node_inventory'),

    # Keysight BMC Associations & Board
    path('keysight/bmc-associations/', keysight_views.keysight_bmc_associations, name='keysight_bmc_associations'),
    path('keysight/bmc-board/', keysight_views.keysight_bmc_board, name='keysight_bmc_board'),
    path('keysight/bmc-board/import/', keysight_views.keysight_bmc_import, name='keysight_bmc_import'),
    path('keysight/bmc-board/delete/', keysight_views.keysight_bmc_delete, name='keysight_bmc_delete'),
    path('keysight/api/deploy/cancel/<int:job_id>/', keysight_views.keysight_deploy_cancel, name='keysight_deploy_cancel'),
    path('keysight/api/deploy/retry/<int:job_id>/', keysight_views.keysight_deploy_retry, name='keysight_deploy_retry'),
    path(
        'keysight/api/changelog/record-operation/',
        keysight_views.keysight_changelog_record_operation,
        name='keysight_changelog_record_operation',
    ),
    path('keysight/api/chassis/<int:chassis_id>/deploy/versions/', keysight_views.keysight_deploy_available_versions, name='keysight_deploy_available_versions'),
    path('keysight/api/chassis/<int:chassis_id>/deploy/installed/', keysight_views.keysight_deploy_installed, name='keysight_deploy_installed'),
    path('keysight/api/deploy/refresh-builds/', keysight_views.keysight_deploy_refresh_builds, name='keysight_deploy_refresh_builds'),
    path('api/nexus/graph-query/', views.api_nexus_graph_query, name='api_nexus_graph_query'),
    path('api/nexus/plugin-status/', views.api_nexus_plugin_status, name='api_nexus_plugin_status'),
    path('api/nexus/sessions/', views.api_nexus_sessions, name='api_nexus_sessions'),
    # Agent Research Knowledge Store
    path('api/research/', views.api_research_index, name='api_research_index'),
    path('api/research/graph/', views.api_research_graph, name='api_research_graph'),
    path('api/research/rebuild/', views.api_research_rebuild, name='api_research_rebuild'),
    path('api/research/<str:output_id>/', views.api_research_output, name='api_research_output')]

# Serve uploaded media files (KCOS packages) in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
