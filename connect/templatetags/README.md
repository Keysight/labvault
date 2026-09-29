# `connect/templatetags/` — template tag libraries

Load in templates with `{% load ip_display %}` or `{% load sidebar_tags %}`.

| File | Tags / filters | Purpose |
|---|---|---|
| `__init__.py` | — | Package marker. |
| `ip_display.py` | filters `mgmt_display`, `mgmt_label`, `mgmt_https_url`, `hardware_login_url`, `hardware_login_for`, `labvault_detail_path`; inclusion tag `mgmt_address_links` | Dual-stack management address display and links. The primary label links into LabVault (`/device/<id>/`, `/keysight/chassis/<id>/`); the address row links to the hardware's own HTTPS UI, using a PTR hostname when one resolves. Renders `connect/_mgmt_address_links.html`. |
| `sidebar_tags.py` | simple tag `sidebar_group_id` | Slugifies a chassis-type label (e.g. `APS-M8400` → `aps-m8400`) for sidebar DOM ids in `base.html`. |

## How it connects

- `ip_display` delegates to `connect/hardware_links.py` (URL building, reverse DNS) and
  `connect/ip_addressing.py` (IP validation, IPv6 bracketing). Model objects are expected to
  expose `mgmt_display`, `mgmt_label`, `connect_address` or `ip_address` (see `Device` and
  `KeysightChassis` in `connect/models.py`).
- `mgmt_address_links` may perform a reverse DNS lookup per rendered object, so avoid it in
  very large loops.
- `sidebar_group_id` is used only by the KEYSIGHT / IXIA group in `connect/templates/connect/base.html`.

Full description: [docs/development/subsystems/core-web.md](../../docs/development/subsystems/core-web.md).
