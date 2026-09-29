"""Minimal SNMP stubs — the full SNMP stack is hard-dumped on the customer SKU.

``snmp_get`` always raises ``RuntimeError`` and ``snmp_walk`` always returns ``[]``.
Callers (``KeysightDriver`` and its subclasses ``OcsDriver`` / ``MellanoxDriver``,
``F5Driver`` LLDP) therefore see no SNMP data. No network I/O.
"""


def snmp_get(*args, **kwargs):
    """Stub: always raises ``RuntimeError`` on the customer SKU."""
    raise RuntimeError("SNMP helpers are not shipped on the customer SKU")


def snmp_walk(*args, **kwargs):
    """Stub: always returns an empty list on the customer SKU."""
    return []
