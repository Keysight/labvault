"""
Multi-vendor device driver framework for ``Device`` rows (switches, firewalls, OCS).

Each vendor driver subclasses :class:`~connect.drivers.base.BaseDriver` and returns
:class:`~connect.drivers.base.DriverResult` objects for:

- Connectivity probing and authentication (``probe()`` → ``'ok' | 'auth_failed' | 'unreachable'``)
- Version/system info retrieval
- Interface listing
- Health monitoring (CPU, memory, uptime)
- Routing table
- VLAN information
- Running/startup config
- Read-only command execution (per-driver prefix allowlist)
- Configuration push (``send_config``) and LLDP enablement

Registered vendors (``VENDOR_DRIVERS``, keyed by ``Device.vendor_type``):

- ``arista``    — Arista EOS (eAPI JSON-RPC over HTTPS/HTTP, SSH + FastCli fallback)
- ``sonic``     — SONiC (RESTCONF + ``/api/v1/cli`` over HTTPS/HTTP, SSH fallback)
- ``fortigate`` — FortiGate (FortiOS REST with API token or session login)
- ``paloalto``  — Palo Alto (PAN-OS XML API with API key / keygen)
- ``keysight``  — Keysight hardware exposing LLDP-MIB over SNMP
- ``ocs``       — Optical circuit switch (Calient-style REST, TL1 fallback for restores)

``f5.F5Driver`` and ``mellanox.MellanoxDriver`` exist as modules but are **not**
registered here, so ``get_driver`` never returns them for built-in vendor types.

Keysight/Ixia *chassis* (``KeysightChassis`` rows) use a separate factory:
:func:`connect.keysight_drivers.get_driver`.

See ``docs/development/subsystems/drivers.md`` for the full contract.
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
    """Returned for unknown vendor_type. All calls return graceful failure.

    Note: ``probe()`` returns the device address string rather than one of the
    standard probe statuses; callers that compare against ``'auth_failed'`` /
    ``'unreachable'`` will treat it as reachable.
    """

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
    """Get the appropriate driver for a device based on its vendor_type.

    Delegates to :func:`connect.driver_registry.resolve_driver` (plugin-aware;
    plugins are off by default). If the registry import or resolution raises,
    falls back to the built-in ``VENDOR_DRIVERS`` map. Unknown vendors get a
    :class:`NullDriver`. Construction performs no network I/O.
    """
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


# Must stay in sync with ``Device.VENDOR_CHOICES`` in connect/models.py (separate copy).
VENDOR_CHOICES = [
    ('arista', 'Arista EOS'),
    ('sonic', 'SONiC'),
    ('fortigate', 'FortiGate'),
    ('paloalto', 'Palo Alto'),
    ('keysight', 'Keysight'),
    ('ocs', 'OCS Photonic'),
]

# Quick command suggestions per vendor, rendered on the device detail page
# (``views.device_detail`` context ``vendor_commands``). Suggestions only — they are
# not an execution allowlist; each driver's ``execute_command`` enforces its own
# prefix check, and the free-form shell views are not shipped on this SKU.
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
