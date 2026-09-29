"""LabVault vs hardware (HTTPS) URLs for devices and Keysight chassis.

Builds the "open device web UI" link (``https://<ptr-name-or-ip>/``) and the in-app
detail path for topology nodes and templates. Only network activity is a cached
reverse-DNS (PTR) lookup; no driver calls, no credentials. Consumers include
``templatetags.ip_display`` and ``keysight_views``.
"""

from __future__ import annotations

import socket
from functools import lru_cache
from typing import Any

from .ip_addressing import bracket_host, is_fqdn_hostname, is_valid_ip, normalize_ip

__all__ = [
    'hardware_login_url',
    'hardware_login_url_for_object',
    'hardware_web_host',
    'mgmt_address_for',
    'labvault_detail_path',
    'enrich_topology_node',
    'reverse_dns_hostname',
]


@lru_cache(maxsize=1024)
def reverse_dns_hostname(ip: str) -> str:
    """Reverse DNS (PTR) for *ip*; returns FQDN or empty. Cached in-process."""
    addr = (normalize_ip(ip) or (ip or '')).strip()
    if not addr or not is_valid_ip(addr):
        return ''
    try:
        hostname, _, _ = socket.gethostbyaddr(addr)
        return (hostname or '').strip()
    except (socket.herror, socket.gaierror, OSError):
        return ''


def hardware_web_host(mgmt_addr: str, *, resolved_hostname: str = '') -> str:
    """Host for hardware HTTPS URL: PTR name when resolved, else management IP/address."""
    resolved = (resolved_hostname or '').strip()
    if resolved and is_fqdn_hostname(resolved):
        return resolved

    mgmt = (mgmt_addr or '').strip()
    if not mgmt:
        return ''

    if is_valid_ip(mgmt):
        ptr = reverse_dns_hostname(mgmt)
        if ptr and is_fqdn_hostname(ptr):
            return ptr
        return normalize_ip(mgmt) or mgmt

    if is_fqdn_hostname(mgmt):
        return mgmt
    return mgmt


def hardware_login_url(addr: str, *, resolved_hostname: str = '', scheme: str = 'https') -> str:
    """HTTPS URL to the device/chassis web UI (hostname when PTR resolves, else IP)."""
    host = hardware_web_host(addr, resolved_hostname=resolved_hostname)
    if not host or ' ' in host:
        return ''
    host = bracket_host(host)
    if not host:
        return ''
    sch = (scheme or 'https').strip().rstrip(':/') or 'https'
    return f'{sch}://{host}/'


def _ptr_lookup_ip(obj: Any) -> str:
    """IPv4/IPv6 literal used for PTR when resolving hardware login host."""
    if obj is None:
        return ''
    for attr in ('ip_address', 'connect_address'):
        raw = (getattr(obj, attr, None) or '').strip()
        if raw and is_valid_ip(raw):
            return normalize_ip(raw) or raw
    mgmt = mgmt_address_for(obj)
    if mgmt and is_valid_ip(mgmt):
        return normalize_ip(mgmt) or mgmt
    return ''


def hardware_login_url_for_object(obj: Any, *, scheme: str = 'https') -> str:
    """Hardware HTTPS URL for a model instance (PTR preferred over raw IP)."""
    mgmt = mgmt_address_for(obj)
    if not mgmt:
        return ''
    ptr_ip = _ptr_lookup_ip(obj)
    resolved = reverse_dns_hostname(ptr_ip) if ptr_ip else ''
    return hardware_login_url(mgmt, resolved_hostname=resolved, scheme=scheme)


def mgmt_address_for(obj: Any) -> str:
    """Primary management address for hardware/login link targets (not dual-stack label text)."""
    if obj is None:
        return ''
    return (
        getattr(obj, 'connect_address', None)
        or getattr(obj, 'ip_address', None)
        or getattr(obj, 'mgmt_display', None)
        or ''
    ).strip()


def labvault_detail_path(obj: Any) -> str:
    """In-app detail path, or '' if unknown."""
    if obj is None or not getattr(obj, 'pk', None):
        return ''
    from .models import Device, KeysightChassis

    if isinstance(obj, Device):
        return f'/device/{obj.pk}/'
    if isinstance(obj, KeysightChassis):
        return f'/keysight/chassis/{obj.pk}/'
    return ''


def enrich_topology_node(
    node: dict,
    *,
    device: Any = None,
    chassis: Any = None,
) -> None:
    """Add mgmt_display, hardware_login_url, and labvault_url for map/inspector UIs."""
    obj = device or chassis
    if not obj:
        return
    mgmt = mgmt_address_for(obj)
    if mgmt:
        node['mgmt_display'] = mgmt
        ptr_ip = _ptr_lookup_ip(obj)
        resolved = reverse_dns_hostname(ptr_ip) if ptr_ip else ''
        node['hardware_login_url'] = hardware_login_url(mgmt, resolved_hostname=resolved)
    path = labvault_detail_path(obj)
    if path:
        node['labvault_url'] = path

