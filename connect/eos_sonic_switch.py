"""
Arista switches that **alternate** between **EOS (eAPI)** and **SONiC** on the
same management IP: automatically align ``Device.vendor_type`` with the stack
that currently answers the API.

**Opt-in** via comma-separated **tags** on the ``Device``:
- ``eos-sonic`` (preferred)
- ``eos_sonic``
- ``dual-os``

When a tagged device's primary probe fails, we probe the alternate driver; if
that succeeds, we persist ``vendor_type`` to match (e.g. ``arista`` ↔ ``sonic``).
"""
import logging
from typing import Set

from django.utils import timezone

logger = logging.getLogger(__name__)

DUAL_STACK_TAGS: Set[str] = frozenset({
    'eos-sonic', 'eos_sonic', 'dual-os', 'eos-sonic-rotate',
})


def device_has_eos_sonic_tag(device) -> bool:
    tags = []
    if getattr(device, 'tags', None):
        tags = [t.strip() for t in (device.tags or '').split(',') if t.strip()]
    return bool(DUAL_STACK_TAGS & set(t.lower() for t in tags))


def _probe_with_vendor_type(device, vendor_type: str) -> str:
    """Temporarily set vendor_type, probe, restore. Returns probe result string."""
    old = device.vendor_type
    device.vendor_type = vendor_type
    try:
        from connect.drivers import get_driver
        return get_driver(device).probe()
    finally:
        device.vendor_type = old


def ensure_eos_sonic_vendor_type(device) -> bool:
    """
    If the device is tagged and ``vendor_type`` is arista/sonic, probe the
    current driver; on failure, try the other. If the other answers ``ok``,
    save ``device.vendor_type`` and return True.
    """
    if not device_has_eos_sonic_tag(device):
        return False
    vt = (getattr(device, 'vendor_type', None) or '').strip()
    if vt not in ('arista', 'sonic'):
        return False

    from connect.drivers import get_driver
    r0 = get_driver(device).probe()
    if r0 == 'ok':
        return False

    other = 'sonic' if vt == 'arista' else 'arista'
    r1 = _probe_with_vendor_type(device, other)
    if r1 == 'ok':
        device.vendor_type = other
        device.save(update_fields=['vendor_type', 'updated_at'])
        logger.info(
            'EOS/SONiC auto: %s vendor_type %s -> %s (first probe was %s)',
            device.ip_address, vt, other, r0,
        )
        return True
    if r0 == 'auth_failed' and r1 == 'auth_failed':
        logger.debug(
            'EOS/SONiC %s: both stacks auth_failed (credentials may be wrong for both API styles)',
            device.ip_address,
        )
    return False
