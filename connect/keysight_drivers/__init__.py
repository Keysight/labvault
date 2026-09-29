"""Driver factory for Keysight/Ixia chassis (``KeysightChassis`` rows).

Unlike :mod:`connect.drivers`, these drivers do not subclass ``BaseDriver``; they
share a duck-typed interface (``probe``, ``get_chassis_info``, ``get_cards``,
``get_ports``, ``get_health``, ``get_sensors``, ``get_licenses``, ``get_lldp_ssh``,
port/card operations, snapshot stubs) and return
:class:`connect.keysight_drivers.ixos.DriverResult`.

Selection is by ``KeysightChassis.chassis_type``: values in ``KCOS_TYPES`` get
:class:`~connect.keysight_drivers.kcos.KCOSDriver`; everything else gets
:class:`~connect.keysight_drivers.ixos.IxOSDriver`. ``bps.BPSDriver`` is not
returned here; KCOS code instantiates it directly when it needs BPS topology.
"""
from .ixos import IxOSDriver
from .kcos import KCOSDriver

# Chassis types that run KCOS instead of IxOS
KCOS_TYPES = {'aps_m1010', 'aps_m8400', 'aps_standalone', 'aresone_htrex', 'trex'}


def _build_driver(chassis, host: str):
    """Instantiate the KCOS or IxOS driver for *host* using the chassis credentials."""
    if chassis.chassis_type in KCOS_TYPES:
        return KCOSDriver(
            ip=host,
            username=chassis.username,
            password=chassis.password,
        )
    return IxOSDriver(
        ip=host,
        username=chassis.username,
        password=chassis.password,
        hostname=(chassis.hostname or '').strip(),
        chassis_type=(chassis.chassis_type or '').strip(),
    )


def get_driver(chassis):
    """Return the appropriate driver for the given chassis model object.
    APS chassis use the KCOS driver; everything else uses IxOS.
    With dual-stack addressing, tries each target until one responds.

    Side effect: with more than one connect target this performs a live ``probe()``
    (auth request) per target; with a single target no network I/O happens here.
    If no target probes ``ok`` the first target's driver is returned anyway."""
    targets = chassis.connect_targets
    if len(targets) <= 1:
        return _build_driver(chassis, targets[0] if targets else chassis.connect_address)
    for host in targets:
        drv = _build_driver(chassis, host)
        if drv.probe() == 'ok':
            return drv
    return _build_driver(chassis, targets[0])
