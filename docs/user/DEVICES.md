# Devices

Devices are generic managed nodes: create/edit/delete, health, protocol/environment views where supported, and configuration backups. Free-form device shells are not shipped.

| Action | Where |
|--------|--------|
| List / add | Dashboard / Add Device |
| Detail / health | Device pages |
| Live status AJAX | `/api/live-status/` |
| REST | `/api/devices/`, `/api/device/<id>/` |
| CLI | `device list` (`limit=` optional) |

Pair devices with lab topologies and fabric maps for end-to-end lab modeling.
