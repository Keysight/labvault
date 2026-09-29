"""
Base driver interface for all vendor device drivers.

Core methods raise ``NotImplementedError`` here and must be overridden:
``probe()``, ``get_base_url()``, ``get_system_info()``, ``get_interfaces()``,
``get_health()``, ``get_routes()``, ``get_vlans()``, ``get_running_config()``,
``get_startup_config()``, ``execute_command()``.

Optional methods have safe defaults: ``send_config`` / ``enable_lldp`` and the
firewall helpers return ``success=False``; the "enhanced" collectors (BGP, OSPF,
environment, counters, DOM, port-channel) return ``success=True`` with empty data;
``get_lldp_neighbors_detail`` delegates to ``get_lldp_neighbors``.

Drivers never raise for device-side failures in normal use — they return a
:class:`DriverResult` with ``success=False`` and ``error`` set.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


@dataclass
class DriverResult:
    """Standardized result from driver operations.

    ``data`` shape depends on the method (see each method docstring), e.g. a dict
    for ``get_system_info`` / ``get_interfaces`` / ``get_health``, a list of dicts
    for routes / LLDP / ARP, or a string for config and command output. On failure
    ``success`` is False and ``error`` holds a human-readable message (``data`` may
    still carry partial results).
    """
    success: bool = False
    data: Any = None
    error: str = ''


class BaseDriver:
    """
    Abstract base driver that all vendor drivers must implement.
    Provides a uniform interface for multi-vendor device management.

    The constructor copies connection settings from the ``Device`` row and does no
    network I/O: ``connect_targets`` (dual-stack ordered list; falls back to
    ``connect_address`` or ``ip_address``), ``username``, ``password``, ``api_key``
    (literal token or a JSON options object, interpreted per driver), ``api_port``
    and ``transport`` (``auto`` / ``https`` / ``http`` / ``ssh``). ``self.ip`` is the
    first connect target; drivers that fail over reassign it while iterating.
    """

    VENDOR_NAME = 'generic'

    def __init__(self, device):
        self.device = device
        targets = getattr(device, 'connect_targets', None)
        if not targets:
            primary = getattr(device, 'connect_address', None) or device.ip_address
            targets = [primary] if primary else []
        self._connect_targets = list(targets)
        self.ip = self._connect_targets[0] if self._connect_targets else device.ip_address
        self.username = device.username
        self.password = device.password
        self.api_key = getattr(device, 'api_key', '') or ''
        self.api_port = getattr(device, 'api_port', None)
        self.transport = getattr(device, 'transport', 'auto') or 'auto'

    def iter_connect_targets(self):
        """Yield management addresses in dual-stack failover order."""
        for addr in self._connect_targets:
            yield addr

    # ---- Connectivity ----

    def probe(self) -> str:
        """Probe device connectivity. Returns: 'ok', 'auth_failed', or 'unreachable'"""
        raise NotImplementedError

    def get_base_url(self, proto=None) -> str:
        """Get the base URL for API calls."""
        raise NotImplementedError

    # ---- System Info ----

    def get_system_info(self) -> DriverResult:
        """Fetch system information.

        ``data`` keys consumed by ``views.fetch_device_data``: ``hostname``,
        ``version``, ``serial_number``, ``model_name``, ``mac_address``, ``uptime``
        (seconds or None). Drivers may add extras such as ``vendor_detail``.
        """
        raise NotImplementedError

    # ---- Interfaces ----

    def get_interfaces(self) -> DriverResult:
        """Fetch all interfaces.

        ``data`` is a dict of lists: ``physical_data``, ``vlan_data``,
        ``port_channel_data``, ``management_data`` (some drivers use
        ``logical_data``). Each row typically has ``name``, ``short_name``,
        ``status``, ``admin_status``, ``status_color``, ``speed_label`` and optional
        ``description`` / ``mtu`` / ``mac`` / ``alias`` / ``display_name``.
        """
        raise NotImplementedError

    # ---- Health ----

    def get_health(self) -> DriverResult:
        """Fetch health: cpu_utilization, memory_used, memory_total, memory_percent, uptime, temperature"""
        raise NotImplementedError

    # ---- Routing ----

    def get_routes(self) -> DriverResult:
        """Fetch routing table: list of {vrf, prefix, protocol, next_hop, interface, metric, preference}"""
        raise NotImplementedError

    # ---- VLANs ----

    def get_vlans(self) -> DriverResult:
        """Fetch VLAN database: list of {id, name, status, interfaces}"""
        raise NotImplementedError

    # ---- Configuration ----

    def get_running_config(self) -> DriverResult:
        """Fetch running configuration as text."""
        raise NotImplementedError

    def get_startup_config(self) -> DriverResult:
        """Fetch startup/saved configuration as text."""
        raise NotImplementedError

    # ---- Command Execution ----

    def execute_command(self, command: str) -> DriverResult:
        """Execute a read-only command on the device. Returns text output.

        Implementations reject commands that do not start with an allowed prefix
        (usually ``show``). There is no free-form shell entry point on this SKU.
        """
        raise NotImplementedError

    # ---- Configuration Push ----

    def send_config(self, commands: list, commit: bool = True) -> DriverResult:
        """Push configuration commands to the device.
        Args:
            commands: List of configuration commands/statements.
            commit: Whether to save/commit after applying.
        Returns:
            DriverResult with success status and output/errors.
        """
        return DriverResult(success=False, error='Config push not supported by this vendor driver')

    def enable_lldp(self) -> DriverResult:
        """Enable LLDP globally and on all physical interfaces.
        Vendor-specific implementation handles the correct API/CLI calls.
        Returns:
            DriverResult with data={'enabled_count': N, 'details': [...]}
        """
        return DriverResult(success=False, error='LLDP enablement not supported by this vendor driver')

    # ---- Security Policies (Firewall-specific) ----

    def get_security_policies(self) -> DriverResult:
        return DriverResult(success=False, error='Not supported by this vendor')

    # ---- VPN/Tunnels (Firewall-specific) ----

    def get_vpn_tunnels(self) -> DriverResult:
        return DriverResult(success=False, error='Not supported by this vendor')

    # ---- HA Status ----

    def get_ha_status(self) -> DriverResult:
        return DriverResult(success=False, error='Not supported by this vendor')

    # ---- ARP Table ----

    def get_arp_table(self) -> DriverResult:
        return DriverResult(success=False, error='Not supported by this vendor')

    # ---- MAC Table ----

    def get_mac_table(self) -> DriverResult:
        return DriverResult(success=False, error='Not supported by this vendor')

    # ---- LLDP Neighbors ----

    def get_lldp_neighbors(self) -> DriverResult:
        """LLDP neighbors: list of {local_port, remote_device, remote_port, ...}."""
        return DriverResult(success=False, error='Not supported by this vendor')

    # ---- Enhanced: LLDP Neighbors Detail (with chassis-id, mgmt-ip) ----

    def get_lldp_neighbors_detail(self) -> DriverResult:
        """Fetch detailed LLDP info including chassis-id, management-address, system-name.

        ``data`` rows: ``local_port``, ``remote_device``, ``remote_port``,
        ``chassis_id``, ``mgmt_ip`` (plus optional ``system_description`` /
        ``source``). Used by device detail and topology discovery.
        """
        return self.get_lldp_neighbors()

    # ---- Enhanced: BGP Summary ----

    def get_bgp_summary(self) -> DriverResult:
        """Fetch BGP peer summary: list of {neighbor, asn, state, prefixes_received, uptime, vrf}"""
        return DriverResult(success=True, data=[])

    # ---- Enhanced: OSPF Neighbors ----

    def get_ospf_neighbors(self) -> DriverResult:
        """Fetch OSPF adjacency table: list of {neighbor_id, address, state, interface, area, priority}"""
        return DriverResult(success=True, data=[])

    # ---- Enhanced: Environment (temp, fans, PSU) ----

    def get_environment(self) -> DriverResult:
        """Fetch environment data: {sensors: [...], fans: [...], power_supplies: [...]}"""
        return DriverResult(success=True, data={'sensors': [], 'fans': [], 'power_supplies': []})

    # ---- Enhanced: Interface Counters (for trending) ----

    def get_interface_counters(self) -> DriverResult:
        """Fetch interface counters: list of {name, bytes_in/out, packets_in/out, errors_in/out}"""
        return DriverResult(success=True, data=[])

    # ---- Enhanced: Port-Channel Members ----

    def get_port_channel_members(self) -> DriverResult:
        """Fetch LAG member interfaces: {po_name: [member1, member2, ...], ...}"""
        return DriverResult(success=True, data={})

    # ---- Enhanced: DOM / Transceiver Diagnostics ----

    def get_dom_info(self) -> DriverResult:
        """Fetch DOM (optical transceiver) info: list of dicts with power, temp, voltage."""
        return DriverResult(success=True, data=[])

    # ---- Helpers ----

    @staticmethod
    def _speed_label(bandwidth):
        """Map bandwidth in bits/s to a short label ('100G', '25G', '100M', '')."""
        if bandwidth >= 1_600_000_000_000:
            return '1.6T'
        elif bandwidth >= 800_000_000_000:
            return '800G'
        elif bandwidth >= 400_000_000_000:
            return '400G'
        elif bandwidth >= 200_000_000_000:
            return '200G'
        elif bandwidth >= 100_000_000_000:
            return '100G'
        elif bandwidth >= 40_000_000_000:
            return '40G'
        elif bandwidth >= 25_000_000_000:
            return '25G'
        elif bandwidth >= 10_000_000_000:
            return '10G'
        elif bandwidth >= 1_000_000_000:
            return '1G'
        elif bandwidth > 0:
            return f'{bandwidth // 1_000_000}M'
        return ''

    @staticmethod
    def _status_color(link_status, admin_status):
        """LED color for the port panel: green / yellow / amber / off / red."""
        if link_status == 'up':
            return 'green'
        elif admin_status in ('disabled', 'adminDown', 'down'):
            return 'yellow'
        elif admin_status == 'errdisabled':
            return 'amber'
        elif link_status in ('notPresent', 'not-present'):
            return 'off'
        return 'red'

    @staticmethod
    def _short_name(name):
        """Extract a short label from a full interface name for chassis panel display."""
        import re
        abbreviations = [
            (r'^HundredGigabitEthernet', 'Hu'),
            (r'^HundredGigE', 'Hu'),
            (r'^FortyGigabitEthernet', 'Fo'),
            (r'^FortyGigE', 'Fo'),
            (r'^TwentyFiveGigE', 'Twe'),
            (r'^TenGigabitEthernet', 'Te'),
            (r'^TenGigE', 'Te'),
            (r'^GigabitEthernet', 'Gi'),
            (r'^FastEthernet', 'Fa'),
            (r'^Ethernet', 'Et'),
            (r'^ethernet', 'eth'),
            (r'^Management', 'Mgmt'),
            (r'^management', 'mgmt'),
            (r'^Port-Channel', 'Po'),
            (r'^PortChannel', 'Po'),
            (r'^port-channel', 'po'),
            (r'^Loopback', 'Lo'),
            (r'^Vlan', 'Vl'),
            (r'^Tunnel', 'Tu'),
        ]
        for pattern, abbr in abbreviations:
            if re.match(pattern, name):
                return re.sub(pattern, abbr, name)
        return name
