# `connect/templates/` — Django templates

Found through `APP_DIRS=True`; every template name starts with `connect/`. Almost every page
extends `connect/base.html` and fills `{% block page_title %}` and `{% block content %}`.

## Layout

| Path | What it is |
|---|---|
| `connect/base.html` | Shell: sidebar (Main, Devices, Config, Data, Keysight / Ixia, Admin), topbar, flash messages, theme/brand switching. Blocks: `title`, `extra_css`, `head_extra`, `body_attrs`, `topbar`, `topbar_class`, `page_title`, `topbar_actions`, `content_area_class`, `content`, `extra_js`. Loads Bootstrap 5.3 and Font Awesome 6.4 from CDNs. |
| `connect/login.html` | Standalone login page (does not extend `base.html`); shows an LDAP hint when `ldap_enabled`. |
| `connect/auth/` | `password_change_form.html`, `password_change_done.html`, `breakglass_reset_password.html` (rendered by `connect/auth_views.py` and `labvault/urls.py`). |
| `connect/includes/` | `lab_topology_topbar.html` (Designer / Fabric / Port Fabric / Usage tabs, `active_view` + `sub_nav`), `lab_topology_insights_subnav.html`. |
| `connect/keysight/` | Keysight chassis pages rendered by `connect/keysight_views.py`; `_`-prefixed files are partials (`_dashboard_filters.html`, `_node_hw_error_controls.html`, `_slot_lldp_section.html`). |
| `connect/_mgmt_address_links.html` | Partial for the `{% mgmt_address_links %}` inclusion tag (`connect/templatetags/ip_display.py`). |

## Page → view map

| Template(s) | View module |
|---|---|
| `dashboard`, `device_detail`, `device_{arp,bgp,config,dom,environment,lldp,mac,ospf,policies,routing,vlans,vpn}`, `add_device`, `edit_device`, `device_compare`, `alerts`, `compliance`, `config_search`, `config_timeline`, `topology`, `fleet_report`, `inventory_report`, `sla_report`, `change_log_report`, `settings`, `about`, `export_labvault`, `import_devices`, `audit_log`, `access_by_ip`, `login` | `connect/views.py` |
| `lab_topology_list`, `lab_topology_detail`, `lab_fabric_map`, `lab_port_fabric`, `lab_topology_usage`, `test_setup_builder` | `connect/lab_topology_views.py` |
| `lab_topology_onboard` | `connect/lab_topology_onboard.py` |
| `lab_topology_usage_insights` | `connect/lab_usage_insights_views.py` |
| `lab_topology_usage_graph` | `connect/lab_usage_graph_views.py` |
| `diagnostics_center` | `connect/diagnostics_views.py` |
| `fleet_swagger` (standalone) | `connect/fleet_api_views.py` |
| `labvault_cli` (standalone) | `connect/labvault_cli_views.py` |
| `keysight/*` | `connect/keysight_views.py` |
| `auth/*` | `connect/auth_views.py`, `labvault/urls.py` |

## Context every template receives

From `connect.context_processors.global_context`: `labvault_customer_sku`,
`labvault_cli_enabled`, `product_name`, `support_no_sla`, `password_change_allowed`,
`show_breakglass_password_reset`. Plus Django's `request`, `user`, `perms`, `messages`.

The sidebar's DEVICES list reads `devices` from the **view's** context, so device-area views
pass `'devices': Device.objects.all()`. The KEYSIGHT / IXIA chassis list reads
`keysight_chassis` the same way; no view in this tree was found passing it.

## Conventions

- Active nav state uses `request.resolver_match.url_name`; keep URL names stable.
- Page-specific CSS/JS goes in `{% block extra_css %}` / `{% block extra_js %}` and lives in
  `connect/static/` (see [`../static/README.md`](../static/README.md)).
- `dashboard.html` includes a partial under `{% if demo_nav_enabled %}` that is not shipped;
  the flag is never set in this SKU, so leave it false.

Full description: [docs/development/subsystems/core-web.md](../../docs/development/subsystems/core-web.md).
