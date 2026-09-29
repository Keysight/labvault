# `connect/drivers/` — network device drivers

Drivers for `Device` rows (switches, firewalls, optical circuit switches). Each driver
subclasses `BaseDriver`, is built with `connect.drivers.get_driver(device)`, does no I/O in
its constructor, and returns `DriverResult(success, data, error)`.

Full reference: [docs/development/subsystems/drivers.md](../../docs/development/subsystems/drivers.md).
Keysight / Ixia chassis use a separate family in [`../keysight_drivers/`](../keysight_drivers/README.md).

## Files

| File | Class / purpose | Transport | Registered `vendor_type` |
|---|---|---|---|
| `__init__.py` | `VENDOR_DRIVERS`, `get_driver()`, `NullDriver`, `VENDOR_CHOICES`, `VENDOR_COMMANDS` (UI suggestions only) | — | — |
| `base.py` | `BaseDriver`, `DriverResult`, label/colour helpers | — | — |
| `arista.py` | `AristaDriver` — EOS; dual-OS EOS↔SONiC delegation; `promote_to_eapi()` | eAPI JSON-RPC https→http, SSH + FastCli fallback (pooled session) | `arista` |
| `sonic.py` | `SonicDriver` | RESTCONF → `/api/v1/cli` → SSH | `sonic` |
| `fortigate.py` | `FortiGateDriver` — VDOM-aware | FortiOS REST (token or session login) | `fortigate` |
| `paloalto.py` | `PaloAltoDriver` | PAN-OS XML API (stored key or keygen) | `paloalto` |
| `keysight.py` | `KeysightDriver` — LLDP-MIB; base for OCS/Mellanox | SNMP (stubbed on the customer SKU) | `keysight` |
| `ocs.py` | `OcsDriver` — cross-connect read/write, parallel page fetch, snapshot restore | REST (`/rest`) https→http per connect target | `ocs` |
| `ocs_tl1.py` | TL1 restore fallback when the REST user is read-only | `sshpass` + SSH → TL1 port | (used by `ocs.py`) |
| `f5.py` | `F5Driver` — read-only BIG-IP | iControl REST | not registered |
| `mellanox.py` | `MellanoxDriver` — ONYX | ONYX JSON → SNMP | not registered |
| `rest_adapter.py` | `RestAdapterDriver` — proxies to an operator HTTP service (plugin mode only) | JSON POST | via plugin manifest |

## Contract summary

- `probe()` → `'ok' | 'auth_failed' | 'unreachable'`.
- Must implement: `get_system_info`, `get_interfaces`, `get_health`, `get_routes`,
  `get_vlans`, `get_running_config`, `get_startup_config`, `execute_command`, `get_base_url`.
- Optional with safe defaults: `send_config`, `enable_lldp`, LLDP, BGP/OSPF, environment,
  counters, port-channel members, DOM, ARP/MAC, firewall policies/VPN/HA.
- `execute_command` enforces a prefix allowlist (`show`, or FortiGate
  `get`/`show`/`diagnose`/`exec ping`/`exec traceroute`). No free-form shell exists on this
  SKU — do not add one.
- Iterate `self.iter_connect_targets()` for dual-stack failover; set explicit timeouts; never
  log credentials.

## Safety

- `OcsDriver.send_config` changes the optical fabric. `xconnect_deleteall` clears every
  cross-connect and is only emitted by `restore_patch_snapshot(..., clear_first=True)`.
- The OCS driver has no reboot operation; keep it that way.

## Adding a vendor

Driver module here → `VENDOR_DRIVERS` + `VENDOR_CHOICES` in `__init__.py` →
`Device.VENDOR_CHOICES` in `connect/models.py` (+ migration) → `_BUILTIN` in
`connect/driver_registry.py` → tests in `connect/tests/` → docs. Step-by-step list in the
subsystem doc.
