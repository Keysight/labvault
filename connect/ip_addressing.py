"""
Dual-stack management addressing (IPv4 DHCP + IPv6 DHCPv6/SLAAC/static) for lab hardware.

Existing ``ip_address`` fields remain the primary IPv4 (or hostname) used everywhere
today. Optional ``mgmt_ipv6`` and ``preferred_ip_version`` control which address
drivers use for new connections without breaking legacy rows.
"""
from __future__ import annotations

import ipaddress
import os
import re
from typing import List, Literal, Optional, Tuple

PreferredVersion = Literal['auto', 'ipv4', 'ipv6', 'ipv6_slaac', 'dual']
Ipv6Source = Literal['', 'dhcpv6', 'slaac', 'static']

_LEGACY_PREFS = frozenset({'auto'})


def normalize_preferred(value: str) -> str:
    """Map legacy ``auto`` to ``ipv4``; validate known modes."""
    pref = (value or 'ipv4').strip().lower()
    if pref in _LEGACY_PREFS:
        return 'ipv4'
    if pref in ('ipv4', 'ipv6', 'ipv6_slaac', 'dual'):
        return pref
    return 'ipv4'


def pick_ipv6_for_mode(
    *,
    ipv6: str = '',
    ipv6_source: Ipv6Source = '',
    mode: PreferredVersion = 'ipv6',
) -> str:
    """Choose IPv6 literal for connect/display based on prefer-ipv6 vs SLAAC mode."""
    v6 = normalize_ip(ipv6)
    if not v6:
        return ''
    if mode != 'ipv6_slaac':
        return v6
    if is_link_local(v6):
        return v6
    src = (ipv6_source or '').strip().lower()
    if src == 'slaac' or is_link_local(v6):
        return v6
    return v6

# Optional site DHCPv6 derive. Empty unless LABVAULT_OCS_* env or callers pass args.
OCS_LAB_DHCPV6_PREFIX = (os.environ.get('LABVAULT_OCS_DHCPV6_PREFIX') or '').strip()
_OCS_V4 = tuple(p for p in (os.environ.get('LABVAULT_OCS_IPV4_PREFIX') or '').split('.') if p)
OCS_LAB_IPV4_SUBNET = _OCS_V4 if len(_OCS_V4) == 3 else ('', '', '')


def _strip_wrappers(value: str) -> str:
    s = (value or '').strip()
    if s.startswith('[') and s.endswith(']'):
        return s[1:-1].strip()
    return s


def normalize_ip(value: str) -> str:
    """Return canonical IP string or '' if not a valid IPv4/IPv6 address."""
    raw = _strip_wrappers(value)
    if not raw or ' ' in raw:
        return ''
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return ''


def is_valid_ip(value: str) -> bool:
    return bool(normalize_ip(value))


def is_ipv6(value: str) -> bool:
    n = normalize_ip(value)
    if not n:
        return False
    try:
        return isinstance(ipaddress.ip_address(n), ipaddress.IPv6Address)
    except ValueError:
        return False


def is_ipv4(value: str) -> bool:
    n = normalize_ip(value)
    if not n:
        return False
    try:
        return isinstance(ipaddress.ip_address(n), ipaddress.IPv4Address)
    except ValueError:
        return False


def is_link_local(value: str) -> bool:
    """True for fe80::/10 (typical SLAAC link-local)."""
    n = normalize_ip(value)
    if not n:
        return False
    try:
        addr = ipaddress.ip_address(n)
        return getattr(addr, 'is_link_local', False)
    except ValueError:
        return False


def derive_dhcpv6_ocs_lab(
    ipv4: str,
    *,
    prefix: str = '',
    ipv4_subnet: Optional[Tuple[str, str, str]] = None,
) -> str:
    """Derive a DHCPv6 IID from a site IPv4 prefix when one is configured.

    No lab prefix is baked in. Pass ``prefix`` / ``ipv4_subnet`` or set
    ``LABVAULT_OCS_DHCPV6_PREFIX`` and ``LABVAULT_OCS_IPV4_PREFIX``.
    """
    use_prefix = (prefix or OCS_LAB_DHCPV6_PREFIX).strip()
    subnet = ipv4_subnet or OCS_LAB_IPV4_SUBNET
    if not use_prefix or not subnet or not all(subnet):
        return ''
    parts = (ipv4 or '').strip().split('.')
    if len(parts) != 4 or tuple(parts[:3]) != tuple(subnet):
        return ''
    last = parts[3]
    if not last.isdigit() or not (0 <= int(last) <= 255):
        return ''
    iid = f'{subnet[2]}{last}'
    return normalize_ip(f'{use_prefix}:{iid}') or f'{use_prefix}:{iid}'


def is_fqdn_hostname(value: str) -> bool:
    """True when value looks like a DNS name (not an IP) with a domain suffix."""
    host = (value or '').strip()
    if not host or is_valid_ip(host):
        return False
    return '.' in host and not host.startswith('.')


def resolve_mgmt_ipv6(
    *,
    ipv4: str = '',
    mgmt_ipv6: str = '',
    ipv6_source: Ipv6Source = '',
) -> str:
    """Return configured or derived IPv6 for display / topology labels."""
    raw = normalize_ip(mgmt_ipv6)
    if raw:
        return raw
    src = (ipv6_source or '').strip().lower()
    if src == 'dhcpv6':
        return derive_dhcpv6_ocs_lab(ipv4)
    return ''


def resolve_mgmt_ipv6_for_connect(
    *,
    ipv4: str = '',
    mgmt_ipv6: str = '',
    ipv6_source: Ipv6Source = '',
) -> str:
    """
    IPv6 for SNMP/HTTPS connections.

    Uses only addresses observed on the wire (stored field). Lab DHCPv6 IID
    derivation is for display until a real global is recorded in ``mgmt_ipv6``.
    """
    raw = normalize_ip(mgmt_ipv6)
    if raw:
        return raw
    return ''


def ipv6_display_tag(ipv6: str, *, ipv6_source: str = '') -> str:
    """Short label for UI (DHCPv6 vs SLAAC vs static)."""
    if not normalize_ip(ipv6):
        return ''
    src = (ipv6_source or '').strip().lower()
    if src == 'dhcpv6':
        return 'DHCPv6'
    if src == 'slaac' or is_link_local(ipv6):
        return 'SLAAC'
    return 'v6'


def is_slaac_candidate(value: str) -> bool:
    """Heuristic: global or ULA IPv6 that is not link-local."""
    n = normalize_ip(value)
    if not n or not is_ipv6(n):
        return False
    if is_link_local(n):
        return True
    try:
        addr = ipaddress.ip_address(n)
        return addr.version == 6 and not addr.is_loopback and not addr.is_multicast
    except ValueError:
        return False


def bracket_host(host: str) -> str:
    """Bracket IPv6 literals for URL/host headers; leave IPv4/FQDN unchanged."""
    raw = (host or '').strip()
    if not raw:
        return ''
    if raw.startswith('['):
        return raw
    if is_ipv6(raw):
        return f'[{normalize_ip(raw)}]'
    return raw


def unbracket_host(host: str) -> str:
    return _strip_wrappers(host)


def _unique_targets(out: List[str], addr: str) -> None:
    a = (addr or '').strip()
    if not a:
        return
    n = normalize_ip(a)
    key = n or a
    if key not in out:
        out.append(n or a)


def resolve_connect_targets(
    *,
    ipv4: str = '',
    ipv6: str = '',
    preferred: PreferredVersion = 'auto',
    hostname: str = '',
) -> List[str]:
    """
    Ordered management addresses for connections (SNMP, HTTPS, SSH).

    When ``hostname`` is a FQDN it is tried first (DNS). Otherwise use DHCP IPv4
    and any stored global IPv6 per ``preferred`` mode. Derived DHCPv6 IIDs are
  not used here — only in ``resolve_mgmt_ipv6`` for display.
    """
    v4 = (ipv4 or '').strip()
    if v4 and is_ipv6(v4):
        v4 = ''
    v6 = normalize_ip(ipv6)
    if preferred == 'ipv6_slaac' or normalize_preferred(preferred) == 'ipv6_slaac':
        v6 = pick_ipv6_for_mode(ipv6=v6, ipv6_source='', mode='ipv6_slaac')
    host = (hostname or '').strip()
    fqdn = host if is_fqdn_hostname(host) else ''
    pref = normalize_preferred(preferred)
    out: List[str] = []

    def _push(*addrs: str) -> None:
        for a in addrs:
            _unique_targets(out, a)

    if pref == 'ipv4':
        _push(fqdn, v4, v6)
    elif pref in ('ipv6', 'ipv6_slaac'):
        _push(fqdn, v6, v4)
        if host and not fqdn:
            _push(host)
    elif pref == 'dual':
        _push(fqdn, v4, v6)
        if host and not fqdn:
            _push(host)
    else:
        _push(fqdn, v4, v6)

    return out


def resolve_connect_address(
    *,
    ipv4: str = '',
    ipv6: str = '',
    preferred: PreferredVersion = 'auto',
    hostname: str = '',
) -> str:
    """First connect target from :func:`resolve_connect_targets`."""
    targets = resolve_connect_targets(
        ipv4=ipv4, ipv6=ipv6, preferred=preferred, hostname=hostname,
    )
    return targets[0] if targets else ''


def display_mgmt_address(
    *,
    ipv4: str = '',
    ipv6: str = '',
    preferred: PreferredVersion = 'auto',
    hostname: str = '',
) -> str:
    """
    Primary management address for UI labels (node badges, sidebars, LLDP tables).

    - ``ipv6`` / ``ipv6_slaac``: show IPv6 when configured
    - ``dual``: show ``IPv6 (IPv4)`` when both exist
    - ``ipv4``: IPv4 (or hostname) primary
    """
    v4 = (ipv4 or '').strip()
    if v4 and is_ipv6(v4):
        v4 = ''
    v6 = normalize_ip(ipv6)
    host = (hostname or '').strip()
    pref = normalize_preferred(preferred)
    if pref == 'dual' and v6 and v4:
        return f'{v6} ({v4})'
    if pref in ('ipv6', 'ipv6_slaac') and v6:
        return v6
    if v4:
        return v4
    if v6:
        return v6
    return host


def mgmt_ip_bundle(
    *,
    ipv4: str = '',
    ipv6: str = '',
    preferred: PreferredVersion = 'ipv4',
    hostname: str = '',
    ipv6_source: Ipv6Source = '',
) -> dict:
    """
    Structured management addresses for topology JSON, site configs, and APIs.

    ``device_ip`` remains the canonical IPv4 lookup key when present; ``mgmt_display``
    is what labels and badges should render.
    """
    v4 = (ipv4 or '').strip()
    if v4 and is_ipv6(v4):
        v4 = ''
    v6 = resolve_mgmt_ipv6(
        ipv4=v4, mgmt_ipv6=ipv6, ipv6_source=ipv6_source,
    )
    pref = normalize_preferred(preferred)
    display = display_mgmt_address(
        ipv4=v4, ipv6=v6, preferred=pref, hostname=hostname,
    )
    lookup_ip = v4 or (v6 if not v4 else '')
    return {
        'device_ip': lookup_ip,
        'mgmt_ipv4': v4,
        'mgmt_ipv6': v6,
        'mgmt_display': display,
        'preferred_ip_version': pref,
    }


def mgmt_ip_bundle_from_entity(entity) -> dict:
    """Build bundle from Device, KeysightChassis, or SNMPDevice."""
    if entity is None:
        return mgmt_ip_bundle()
    if hasattr(entity, 'mgmt_ip_fields'):
        return entity.mgmt_ip_fields
    return mgmt_ip_bundle(
        ipv4=getattr(entity, 'ip_address', '') or '',
        ipv6=getattr(entity, 'effective_mgmt_ipv6', None)
        or getattr(entity, 'mgmt_ipv6', '') or '',
        preferred=getattr(entity, 'preferred_ip_version', 'ipv4') or 'ipv4',
        hostname=getattr(entity, 'hostname', '') or '',
        ipv6_source=getattr(entity, 'mgmt_ipv6_source', '') or '',
    )


def mgmt_label_text(
    *,
    ipv4: str = '',
    ipv6: str = '',
    preferred: PreferredVersion = 'auto',
    hostname: str = '',
) -> str:
    """Human label: ``hostname (mgmt)`` or management address alone."""
    host = (hostname or '').strip()
    disp = display_mgmt_address(
        ipv4=ipv4, ipv6=ipv6, preferred=preferred, hostname=hostname,
    )
    if host and disp and host != disp:
        return f'{host} ({disp})'
    return host or disp or ''


def identity_address_keys(
    *,
    ipv4: str = '',
    ipv6: str = '',
    hostname: str = '',
) -> List[str]:
    """Lookup keys for LLDP / topology identity index (IPv4, IPv6, hostname)."""
    keys: List[str] = []
    v4 = (ipv4 or '').strip()
    v6 = normalize_ip(ipv6)
    host = (hostname or '').strip()

    def _add(val: str) -> None:
        v = (val or '').strip()
        if not v:
            return
        if v not in keys:
            keys.append(v)
        low = v.lower()
        if low not in keys:
            keys.append(low)
        if '.' in v:
            short = v.split('.')[0]
            if short.lower() not in keys:
                keys.append(short.lower())

    _add(v4)
    _add(v6)
    _add(unbracket_host(v6))
    _add(host)
    return keys


def enrich_lldp_neighbor_display(
    neighbor: dict,
    identity_index: Optional[dict] = None,
) -> dict:
    """
    Add ``mgmt_ip_display`` (and optional ``mgmt_ipv6``) from inventory when the
    neighbor resolves to a known device/chassis with dual-stack fields.
    """
    out = dict(neighbor or {})
    raw = (out.get('mgmt_ip') or out.get('management_address') or '').strip()
    out['mgmt_ip_raw'] = raw
    entity = None
    if identity_index and raw:
        entity = identity_index.get(raw) or identity_index.get(raw.lower())
    if entity is None and identity_index:
        for field in ('remote_device', 'system_name'):
            val = (out.get(field) or '').strip()
            if val:
                entity = identity_index.get(val.lower()) or identity_index.get(
                    val.split('.')[0].lower(),
                )
                if entity:
                    break
    if entity is not None:
        disp = getattr(entity, 'mgmt_display', None) or getattr(
            entity, 'connect_address', None,
        ) or getattr(entity, 'ip_address', '') or raw
        out['mgmt_ip_display'] = disp
        v6 = normalize_ip(getattr(entity, 'mgmt_ipv6', '') or '')
        if v6:
            out['mgmt_ipv6'] = v6
        v4 = (getattr(entity, 'ip_address', '') or '').strip()
        if v4:
            out['mgmt_ipv4'] = v4
    else:
        out['mgmt_ip_display'] = raw
    return out


def enrich_lldp_neighbors_for_display(
    neighbors: list,
    identity_index: Optional[dict] = None,
) -> list:
    """Attach ``mgmt_ip_display`` from inventory for templates and APIs."""
    if not neighbors:
        return []
    idx = identity_index
    if idx is None:
        from connect.models import Device, KeysightChassis

        idx = {}
        for d in Device.objects.all().only(
            'ip_address', 'mgmt_ipv6', 'hostname', 'preferred_ip_version',
        ):
            for key in identity_address_keys(
                ipv4=d.ip_address,
                ipv6=d.mgmt_ipv6,
                hostname=d.hostname or '',
            ):
                idx[key] = d
        for c in KeysightChassis.objects.all().only(
            'ip_address', 'mgmt_ipv6', 'hostname', 'preferred_ip_version',
        ):
            for key in identity_address_keys(
                ipv4=c.ip_address,
                ipv6=c.mgmt_ipv6,
                hostname=c.hostname or '',
            ):
                idx[key] = c
    return [enrich_lldp_neighbor_display(n, idx) for n in neighbors]


def display_addresses(
    *,
    ipv4: str = '',
    ipv6: str = '',
    hostname: str = '',
) -> str:
    """Human-readable dual-stack summary for UI lists."""
    parts = []
    if (ipv4 or '').strip():
        parts.append(f'v4 {ipv4.strip()}')
    v6n = normalize_ip(ipv6)
    if v6n:
        tag = ipv6_display_tag(v6n)
        parts.append(f'{tag} {v6n}')
    if hostname and hostname.strip() not in (ipv4, v6n):
        parts.append(hostname.strip())
    return ' · '.join(parts) if parts else '—'


def parse_address_input(value: str) -> Tuple[str, str]:
    """
    Parse user input that may contain IPv4 and/or IPv6 (comma or space separated).
    Returns (ipv4_or_host, ipv6).
    """
    raw = (value or '').strip()
    if not raw:
        return '', ''
    tokens = re.split(r'[\s,;]+', raw)
    v4 = ''
    v6 = ''
    host = ''
    for tok in tokens:
        if not tok:
            continue
        n = normalize_ip(tok)
        if n:
            if is_ipv6(n):
                v6 = n
            else:
                v4 = n
        elif not host:
            host = tok
    if not v4 and host and not is_ipv6(host):
        v4 = host
    return v4, v6
