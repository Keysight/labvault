# `connect/keysight_drivers/` — Keysight / Ixia chassis drivers

Drivers for `KeysightChassis` rows. They do **not** subclass `connect.drivers.BaseDriver`;
they share a duck-typed interface and return `connect.keysight_drivers.ixos.DriverResult`
(same fields as the network-driver result, different class).

Full reference: [docs/development/subsystems/drivers.md](../../docs/development/subsystems/drivers.md)
(section "Keysight chassis drivers"). Chassis pages and polling:
[docs/development/subsystems/keysight-chassis.md](../../docs/development/subsystems/keysight-chassis.md).

## Files

| File | Class / purpose | Transport |
|---|---|---|
| `__init__.py` | `get_driver(chassis)` factory; `KCOS_TYPES` | probes each connect target when more than one |
| `ixos.py` | `IxOSDriver` — IxOS chassis (XGS, AresONE, ...): chassis info, cards, ports, perf counters, sensors, LLDP (REST + SSH `show lldp-peer-info`), `show topology`, PCPU health/app versions, licensing, port/card operations, IxOS upgrade; `DriverResult`; `fill_inferred_pcpu_mgmt_ips()` | HTTPS REST (`x-api-key`) + SSH CLI |
| `kcos.py` | `KCOSDriver` — KCOS/APS clusters and HTRex/T-Rex: nodes as cards, connections as ports, Helm deployment info, app switching, BMC power ops, firmware, snapshots, LLDP via root SSH, `reboot_chassis` | HTTPS REST (Keycloak Bearer) + root SSH via `connect/kcos_ssh.py` |
| `bps.py` | `BPSDriver` — BreakingPoint session API, read-only slot/topology data (used by `KCOSDriver.get_lldp_ssh` on M8400) | HTTPS REST |

## Selection

`chassis.chassis_type` in `KCOS_TYPES` (`aps_m1010`, `aps_m8400`, `aps_standalone`,
`aresone_htrex`, `trex`) → `KCOSDriver`; otherwise `IxOSDriver`.

## Contract summary (both drivers)

| Method | `data` |
|---|---|
| `probe()` | `'ok' \| 'auth_failed' \| 'unreachable'` |
| `get_chassis_info()` | dict: `management_ip`, `chassis_type`, `serial_number`, `state`, `num_physical_cards`, `ixos_applications`, ... (KCOS adds `kcos_version`, node counts) |
| `get_cards()` | list of card/slot dicts (`card_number`, `type`, `state`, `num_ports`, ...) |
| `get_ports()` | list of port dicts (`card_number`, `port_number`, `owner`, `link_state`, `led_color`, `speed`, ...) |
| `get_health()` | dict (`cpu_utilization`, `memory_*`; KCOS: node readiness) |
| `get_sensors()`, `get_licenses()`, `get_port_stats()`, `get_services()` | lists / raw JSON |
| `get_lldp_ssh(bps_topology=None, chassis_type='')` | list of `{local_port, remote_device, remote_port, chassis_id, mgmt_ip}` |
| `take_ownership` / `release_ownership` / `reboot_port` / `reset_port` / `hotswap_card` | IxOS: real operations; KCOS: no-op success |
| snapshots | KCOS: real; IxOS: stubs |

## Safety

Mutating methods (port reboot/reset, card hotswap, IxOS upgrade, `enable_lldp_peer_info`
which restarts IxServer, KCOS app switch, node power operations, firmware/Helm deploy,
snapshot restore, `reboot_chassis`) run only when a caller invokes them explicitly. Do not
call them from polling or collection code, and do not trigger fleet `recover` against lab
gear without an operator request.

Credentials come from the `KeysightChassis` row and, for root SSH hops, Django settings
`KCOS_ROOT_SSH_USER` / `KCOS_ROOT_SSH_PASSWORD` and `ARESONE_ROOT_SSH_USER` /
`ARESONE_ROOT_SSH_PASSWORD`. Never log them.
