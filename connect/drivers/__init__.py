"""
Multi-vendor device driver framework.

Each vendor driver implements a common interface for:
- Connectivity probing and authentication
- Version/system info retrieval
- Interface listing
- Health monitoring (CPU, memory, uptime)
- Routing table
- VLAN information
- Running/startup config
- Command execution

Supported vendors:
- Arista EOS (eAPI via JSON-RPC over HTTP/HTTPS)
- SONiC (REST API over HTTPS)
- FortiGate (REST API with API key or session token)
- Palo Alto (XML API with API key)
- OCS (photonic switch REST, SNMP LLDP)
"""

from .base import BaseDriver, DriverResult
from .arista import AristaDriver
from .sonic import SonicDriver
from .fortigate import FortiGateDriver
from .paloalto import PaloAltoDriver
from .keysight import KeysightDriver
from .ocs import OcsDriver

VENDOR_DRIVERS = {
    'arista': AristaDriver,
    'sonic': SonicDriver,
    'fortigate': FortiGateDriver,
    'paloalto': PaloAltoDriver,
    'keysight': KeysightDriver,
    'ocs': OcsDriver,
}


class NullDriver(BaseDriver):
    """Returned for unknown vendor_type. All calls return graceful failure."""

    def __init__(self, device):
        super().__init__(device)
        self._vendor = getattr(device, 'vendor_type', 'unknown') or 'unknown'

    def probe(self):
        return getattr(self.device, 'ip_address', '')

    def get_system_info(self):
        return DriverResult(False, error=f"No driver registered for vendor '{self._vendor}'")

    def get_interfaces(self):
        return DriverResult(False, error=f"No driver registered for vendor '{self._vendor}'")

    def get_lldp_neighbors(self):
        return DriverResult(False, error=f"No driver registered for vendor '{self._vendor}'")

    def send_config(self, commands):
        return DriverResult(False, error=f"No driver registered for vendor '{self._vendor}'")


def get_driver(device):
    """Get the appropriate driver for a device based on its vendor_type."""
    try:
        from connect.driver_registry import resolve_driver
        return resolve_driver(device)
    except Exception:
        # Fall back to built-in map if plugin registry is unavailable.
        vendor = getattr(device, 'vendor_type', None)
        driver_class = VENDOR_DRIVERS.get(vendor)
        if driver_class is None:
            return NullDriver(device)
        return driver_class(device)


VENDOR_CHOICES = [
    ('arista', 'Arista EOS'),
    ('sonic', 'SONiC'),
    ('fortigate', 'FortiGate'),
    ('paloalto', 'Palo Alto'),
    ('keysight', 'Keysight'),
    ('ocs', 'OCS Photonic'),
]

# Quick command suggestions per vendor
VENDOR_COMMANDS = {
    'arista': [
        'show version', 'show interfaces status', 'show ip route',
        'show vlan', 'show ip bgp summary', 'show ip ospf neighbor',
        'show lldp neighbors', 'show inventory', 'show environment all',
        'show running-config', 'show mac address-table',
    ],
    'sonic': [
        'show version', 'show interfaces status', 'show ip route',
        'show vlan brief', 'show ip bgp summary', 'show lldp table',
        'show platform summary', 'show system-memory',
        'show running-configuration', 'show mac',
    ],
    'fortigate': [
        'get system status', 'get system interface', 'get router info routing-table all',
        'get system performance status', 'get system ha status',
        'get firewall policy', 'get vpn ipsec tunnel summary',
        'get log memory filter', 'get system session status',
        'diagnose sys top',
    ],
    'keysight': [
        'snmpwalk -c public 1.0.8802.1.1.2.1.4.1.1.9',
    ],
    'ocs': [
        '{"op": "xconnect_add", "in": "1.1.1/1", "out": "2.1.1/1", "group": "SYSTEM", "conn": "lab_a", "dir": "bi", "band": "CBAND"}',
    ],
    'paloalto': [
        'show system info', 'show interface all', 'show routing route',
        'show session info', 'show running security-policy',
        'show high-availability state', 'show vpn ipsec-sa',
        'show system resources', 'show counter global',
        'show arp all',
    ],
}
