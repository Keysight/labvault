"""Auto-profile topology nodes for timeline metrics and events.

Builds a *resource catalog* for one LabTopology: ``{resource_key: meta}`` where
``resource_key`` is ``node_<pk>`` for nodes and ``node_<pk>__<port_label>`` for
ports. Each entry carries a profile (``keysight_chassis``, ``arista_switch``,
``ocs`` …), the metric names collected for it, and management / chassis-internal
PCPU addresses. Catalogs are cached (``lab_resource_catalog:<topo>:live=<0|1>``,
300 s with live ports, 600 s without) and dropped by
:func:`invalidate_resource_catalog_cache`.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

from django.utils import timezone

from connect.cache_utils import cache_delete, cache_get, cache_set

from connect.models import KeysightChassis, LabTopology, LabTopologyNode
from connect.lab_metrics import TS_DB

PROFILE_KEYSIGHT_CHASSIS = 'keysight_chassis'
PROFILE_KEYSIGHT_PORT = 'keysight_port'
PROFILE_ARISTA_SWITCH = 'arista_switch'
PROFILE_SONIC_SWITCH = 'sonic_switch'
PROFILE_OCS = 'ocs'
PROFILE_GENERIC = 'generic'

CATALOG_CACHE_TTL = 600  # designer + observed keys change slowly; avoids per-request SSH
CATALOG_LIVE_PORTS_CACHE_TTL = 300

CHASSIS_METRICS = ['cpu_pct', 'mem_pct', 'bps_in', 'bps_out']
SWITCH_METRICS = ['cpu_pct', 'mem_pct', 'bps_in', 'bps_out']
PORT_METRICS = ['bps_in', 'bps_out', 'port_ownership', 'link_speed_gbps', 'cpu_pct', 'mem_pct']

_PCPU_MGMT_LABEL_RE = re.compile(r'^(\d+)\.(\d+)$')
_PCPU_MGMT_IP_RE = re.compile(r'^10\.0\.\d+\.\d+$')
PORT_PCPU_MGMT_CACHE_TTL = 3600


def node_resource_key(node: LabTopologyNode) -> str:
    """Metric resource key for a topology node (``node_<pk>``)."""
    return f'node_{node.pk}'


def port_resource_key(node_id: int, port_label: str) -> str:
    """Metric resource key for a node port (``node_<pk>__<label>``)."""
    return f'node_{node_id}__{port_label}'


def _node_mgmt_ip(node: LabTopologyNode) -> str:
    """Best-effort management IPv4 for a topology node."""
    if node.device and (node.device.ip_address or '').strip():
        return str(node.device.ip_address).strip()
    extra = node.extra or {}
    for key in ('device_ip', 'mgmt_ipv4'):
        ip = (extra.get(key) or '').strip()
        if ip:
            return ip
    chassis_id = extra.get('chassis_id')
    if chassis_id:
        ch = KeysightChassis.objects.filter(pk=chassis_id).only('ip_address').first()
        if ch and (ch.ip_address or '').strip():
            return str(ch.ip_address).strip()
    return ''


def _is_rg_physical_port(port_number: Any) -> bool:
    """AresONE logical ports 9–24 use RG notation (e.g. ``2.1``) on the NP."""
    try:
        pn = int(port_number)
    except (TypeError, ValueError):
        return False
    return 9 <= pn <= 24


def chassis_port_metric_label(port: dict) -> str:
    """Canonical port label for metric resource keys (collector, catalog, timeline).

    Prefer fully-qualified names when present. For AresONE RG ports, prefix the
    line-card number so ``2.1`` on card 3 becomes ``3.2.1`` — RG labels repeat
    across cards and must not collide in ``np_timeseries``.
    """
    fqn = (port.get('fully_qualified_port_name') or port.get('fullyQualifiedPortName') or '').strip()
    if fqn and fqn.upper() != 'N/A':
        return fqn

    disp = (port.get('port_display') or '').strip()
    cn = port.get('card_number')
    pn = port.get('port_number')

    if disp and cn is not None and _is_rg_physical_port(pn):
        card_s = str(int(cn))
        if disp.startswith(f'{card_s}.'):
            return disp
        if '.' in disp:
            return f'{card_s}.{disp}'

    if disp:
        return disp

    if cn is not None and pn is not None:
        return f'{cn}.{pn}'

    label = (port.get('label') or port.get('name') or '').strip()
    if label and '__' not in label:
        return label

    return str(cn or pn or 'port')


def pcpu_mgmt_ip_from_port(port: dict) -> str:
    """Resolve internal PCPU management IP from a port row (IxOS ``get_ports`` shape)."""
    mgmt = (port.get('management_ip') or port.get('managementIp') or '').strip()
    if mgmt and _PCPU_MGMT_IP_RE.match(mgmt):
        return mgmt
    cn = port.get('card_number')
    pn = port.get('port_number')
    if cn is not None and pn is not None:
        try:
            return f'10.0.{int(cn)}.{int(pn)}'
        except (TypeError, ValueError):
            pass
    return infer_pcpu_mgmt_ip_from_label(chassis_port_metric_label(port))


def is_aresone_chassis_type(chassis_type: str) -> bool:
    """True for any AresONE chassis_type spelling."""
    return 'aresone' in (chassis_type or '').lower().replace('-', '').replace('_', '')


def infer_pcpu_mgmt_ip_from_label(label: str, *, chassis_type: str = '') -> str:
    """Infer PCPU host IP from a two-part port label.

    XGS ``card.port`` → ``10.0.<card>.<port>`` (1.2 is its own packet CPU).
    AresONE-M ``RG.port`` → ``10.0.1.<RG>`` (1.1–1.8 share RG01 / 10.0.1.1).
    """
    m = _PCPU_MGMT_LABEL_RE.match(str(label or '').strip())
    if not m:
        return ''
    left, right = int(m.group(1)), int(m.group(2))
    if is_aresone_chassis_type(chassis_type):
        return f'10.0.1.{left}'
    return f'10.0.{left}.{right}'


def resolve_pcpu_mgmt_ip(
    label: str,
    *,
    chassis_type: str = '',
    cached: str = '',
    meta_ip: str = '',
) -> str:
    """Prefer AresONE RG mapping over stale IxOS cache entries (1.3 → 10.0.1.3)."""
    inferred = infer_pcpu_mgmt_ip_from_label(label, chassis_type=chassis_type)
    if is_aresone_chassis_type(chassis_type) and inferred:
        return inferred
    return (meta_ip or cached or inferred or '').strip()


def load_port_owner_index(topo_id: int) -> Dict[str, str]:
    """Map port resource_key → IxOS owner name (non-Free)."""
    index: Dict[str, str] = {}
    merged = cache_get(f'port_owner_names:{topo_id}')
    if isinstance(merged, dict):
        index.update(merged)
    nodes = LabTopologyNode.objects.filter(topology_id=topo_id, node_type='chassis').only('pk')
    for node in nodes:
        node_map = cache_get(f'port_owner_names:{topo_id}:{node.pk}')
        if isinstance(node_map, dict):
            index.update(node_map)
    return index


def load_port_pcpu_mgmt_index(topo_id: int, *, live_ssh: bool = False) -> Dict[str, str]:
    """Map port resource_key → PCPU managementIp (10.0.x.x).

    Primary source is per-node maps published by the metric collector. Falls back
    to chassis-detail cache when present. Live ``driver.get_ports()`` SSH is
    opt-in only (``live_ssh=True``) — insights/timeline must not block on it.
    """
    from connect.keysight_drivers.ixos import fill_inferred_pcpu_mgmt_ips
    from connect.keysight_views import _get_cached
    from connect.metric_collectors import _chassis_port_label

    index: Dict[str, str] = {}
    nodes = list(
        LabTopologyNode.objects.filter(topology_id=topo_id, node_type='chassis')
        .select_related('device')
        .only('pk', 'extra', 'device')
    )

    for node in nodes:
        node_map = cache_get(f'pcpu_mgmt_ip:{topo_id}:{node.pk}')
        if isinstance(node_map, dict):
            index.update(node_map)

    if index:
        return index

    cache_key = f'port_mgmt_ip:{topo_id}'
    hit = cache_get(cache_key)
    if isinstance(hit, dict) and hit:
        return dict(hit)

    for node in nodes:
        chassis_id = (node.extra or {}).get('chassis_id')
        if not chassis_id:
            continue
        cached = _get_cached(int(chassis_id)) or {}
        ports = list(cached.get('ports') or [])
        if ports:
            fill_inferred_pcpu_mgmt_ips(ports)
        for port in ports:
            if not isinstance(port, dict):
                continue
            lbl = _chassis_port_label(port).strip()
            mgmt = pcpu_mgmt_ip_from_port(port)
            if lbl and mgmt and _PCPU_MGMT_IP_RE.match(mgmt):
                index[port_resource_key(node.pk, lbl)] = mgmt

    if not index and live_ssh:
        for node in nodes:
            chassis_id = (node.extra or {}).get('chassis_id')
            if not chassis_id:
                continue
            try:
                ch = KeysightChassis.objects.filter(pk=chassis_id).first()
                if not ch:
                    continue
                from connect.keysight_drivers import get_driver

                driver = get_driver(ch)
                ports_res = driver.get_ports()
                if not ports_res.success or not isinstance(ports_res.data, list):
                    continue
                ports = fill_inferred_pcpu_mgmt_ips(list(ports_res.data))
                for port in ports:
                    if not isinstance(port, dict):
                        continue
                    lbl = _chassis_port_label(port).strip()
                    mgmt = pcpu_mgmt_ip_from_port(port)
                    if lbl and mgmt and _PCPU_MGMT_IP_RE.match(mgmt):
                        index[port_resource_key(node.pk, lbl)] = mgmt
            except Exception:
                continue

    if index:
        cache_set(cache_key, index, PORT_PCPU_MGMT_CACHE_TTL)
    return index


def port_pcpu_mgmt_from_catalog(catalog: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Build port resource_key → PCPU mgmt IP from catalog entries (no I/O)."""
    index: Dict[str, str] = {}
    for key, meta in catalog.items():
        parent = meta.get('parent')
        if not parent:
            continue
        ctype = (catalog.get(parent) or {}).get('chassis_type') or ''
        mgmt = resolve_pcpu_mgmt_ip(
            meta.get('label') or '',
            chassis_type=ctype,
            cached=meta.get('pcpu_ip') or '',
        )
        if mgmt:
            index[key] = mgmt
    return index


def attach_mgmt_ips_to_catalog(
    catalog: Dict[str, Dict[str, Any]],
    topo_id: int,
) -> Dict[str, Dict[str, Any]]:
    """Add mgmt_ip / chassis_ip / pcpu_ip on catalog entries for UI + insights."""
    parent_mgmt: Dict[str, str] = {}
    missing_node_ids = {
        meta['node_id']
        for meta in catalog.values()
        if not meta.get('parent') and meta.get('node_id') and not (meta.get('mgmt_ip') or '').strip()
    }
    nodes_by_id: Dict[int, LabTopologyNode] = {}
    if missing_node_ids:
        nodes_by_id = {
            n.pk: n
            for n in LabTopologyNode.objects.filter(pk__in=missing_node_ids).select_related('device')
        }

    for key, meta in catalog.items():
        if meta.get('parent'):
            continue
        mgmt = (meta.get('mgmt_ip') or '').strip()
        if not mgmt and meta.get('node_id'):
            node = nodes_by_id.get(meta['node_id'])
            if node:
                mgmt = _node_mgmt_ip(node)
                if mgmt:
                    meta['mgmt_ip'] = mgmt
        if mgmt:
            parent_mgmt[key] = mgmt

    port_mgmt = load_port_pcpu_mgmt_index(topo_id, live_ssh=False)
    for key, meta in catalog.items():
        parent = meta.get('parent')
        if not parent and meta.get('node_id') and '__' in key:
            parent = f"node_{meta['node_id']}"
            meta['parent'] = parent
        if parent:
            cip = (meta.get('chassis_ip') or parent_mgmt.get(parent) or '').strip()
            if not cip and meta.get('node_id'):
                node_parent = parent_mgmt.get(f"node_{meta['node_id']}") or ''
                cip = node_parent.strip()
            if cip:
                meta['chassis_ip'] = cip
            parent_meta = catalog.get(parent) or {}
            ctype = parent_meta.get('chassis_type') or ''
            pcpu = resolve_pcpu_mgmt_ip(
                meta.get('label') or '',
                chassis_type=ctype,
                cached=port_mgmt.get(key) or '',
                meta_ip=meta.get('pcpu_ip') or '',
            )
            if pcpu:
                meta['pcpu_ip'] = pcpu

    merged_pcpu = port_pcpu_mgmt_from_catalog(catalog)
    if merged_pcpu:
        cache_set(f'port_mgmt_ip:{topo_id}', merged_pcpu, PORT_PCPU_MGMT_CACHE_TTL)
    return catalog


def _profile_for_node(node: LabTopologyNode) -> str:
    extra = node.extra or {}
    if node.node_type == 'chassis' or extra.get('chassis_id'):
        return PROFILE_KEYSIGHT_CHASSIS
    if node.node_type == 'ocs':
        return PROFILE_OCS
    if node.node_type == 'switch' and node.device:
        vendor = (node.device.vendor_type or '').lower()
        if vendor == 'arista':
            return PROFILE_ARISTA_SWITCH
        if vendor == 'sonic':
            return PROFILE_SONIC_SWITCH
    return PROFILE_GENERIC


def _catalog_cache_key(topo_id: int, *, live_ports: bool) -> str:
    return f'lab_resource_catalog:{topo_id}:live={1 if live_ports else 0}'


def build_resource_catalog(topo_id: int, *, live_ports: bool = True) -> Dict[str, Dict[str, Any]]:
    """Return ``{ resource_key: { profile, label, metrics, node_id, parent? } }``.

    When ``live_ports`` is False, chassis port labels come from designer metadata
    (and optional ``port_count`` if the chassis model has it) — no blocking SSH.
    """
    topo = LabTopology.objects.filter(pk=topo_id).first()
    if not topo:
        return {}

    catalog: Dict[str, Dict[str, Any]] = {}
    nodes = list(
        LabTopologyNode.objects.filter(topology_id=topo_id)
        .select_related('device')
        .order_by('pk')
    )

    for node in nodes:
        rkey = node_resource_key(node)
        profile = _profile_for_node(node)
        metrics: List[str] = []
        if profile == PROFILE_KEYSIGHT_CHASSIS:
            metrics = list(CHASSIS_METRICS)
        elif profile in (PROFILE_ARISTA_SWITCH, PROFILE_SONIC_SWITCH):
            metrics = list(SWITCH_METRICS)
        elif profile == PROFILE_OCS:
            metrics = list(PORT_METRICS)

        mgmt_ip = _node_mgmt_ip(node)
        catalog[rkey] = {
            'profile': profile,
            'label': node.label or rkey,
            'metrics': metrics,
            'node_id': node.pk,
            'node_type': node.node_type,
            'mgmt_ip': mgmt_ip,
            'chassis_type': ((node.extra or {}).get('chassis_type') or '').strip(),
        }

        if profile == PROFILE_KEYSIGHT_CHASSIS:
            for port_label in _chassis_port_labels(node, live_ports=live_ports):
                pkey = port_resource_key(node.pk, port_label)
                catalog[pkey] = {
                    'profile': PROFILE_KEYSIGHT_PORT,
                    'label': port_label,
                    'parent': rkey,
                    'metrics': list(PORT_METRICS),
                    'node_id': node.pk,
                    'chassis_ip': mgmt_ip,
                }
        elif profile in (PROFILE_ARISTA_SWITCH, PROFILE_SONIC_SWITCH):
            for port_label in _switch_port_labels(node):
                pkey = port_resource_key(node.pk, port_label)
                catalog[pkey] = {
                    'profile': 'switch_port',
                    'label': port_label,
                    'parent': rkey,
                    'metrics': ['bps_in', 'bps_out', 'input_discards'],
                    'node_id': node.pk,
                    'chassis_ip': mgmt_ip,
                }
        elif profile == PROFILE_OCS:
            for port_label in _ocs_port_labels(node):
                pkey = port_resource_key(node.pk, port_label)
                catalog[pkey] = {
                    'profile': 'ocs_port',
                    'label': port_label,
                    'parent': rkey,
                    'metrics': list(PORT_METRICS),
                    'node_id': node.pk,
                    'chassis_ip': mgmt_ip,
                }

    return catalog


def build_enriched_resource_catalog(
    topo_id: int,
    *,
    live_ports: bool = True,
    use_cache: bool = True,
    observe_db: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """Catalog plus port keys observed in np_timeseries for this topology."""
    cache_key = _catalog_cache_key(topo_id, live_ports=live_ports)
    if use_cache:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

    catalog = build_resource_catalog(topo_id, live_ports=live_ports)
    if observe_db:
        catalog = enrich_catalog_with_observed_keys(catalog, topo_id, recent_hours=48)
    attach_mgmt_ips_to_catalog(catalog, topo_id)
    ttl = CATALOG_LIVE_PORTS_CACHE_TTL if live_ports else CATALOG_CACHE_TTL
    if use_cache:
        cache_set(cache_key, catalog, ttl)
    return catalog


def invalidate_resource_catalog_cache(topo_id: int) -> None:
    """Drop cached catalogs after topology designer edits."""
    cache_delete(_catalog_cache_key(topo_id, live_ports=True))
    cache_delete(_catalog_cache_key(topo_id, live_ports=False))


def apply_observed_keys_to_catalog(
    catalog: Dict[str, Dict[str, Any]],
    observed: Iterable[str],
) -> Dict[str, Dict[str, Any]]:
    """Merge already-known resource keys into a designer catalog (no extra SQL)."""
    node_profiles: Dict[int, str] = {}
    live_port_labels_by_node: Dict[int, Set[str]] = {}
    for meta in catalog.values():
        nid = meta.get('node_id')
        if nid and not meta.get('parent'):
            node_profiles[nid] = meta.get('profile', PROFILE_GENERIC)
        if nid and meta.get('profile') == PROFILE_KEYSIGHT_PORT:
            lbl = (meta.get('label') or '').strip()
            if lbl:
                live_port_labels_by_node.setdefault(nid, set()).add(lbl)

    for key in observed:
        if key in catalog:
            continue
        if '__' not in key:
            continue
        node_part, port_label = key.split('__', 1)
        if not node_part.startswith('node_'):
            continue
        try:
            node_id = int(node_part.replace('node_', '', 1))
        except ValueError:
            continue
        parent_key = node_part
        parent_profile = node_profiles.get(node_id) or catalog.get(parent_key, {}).get('profile')
        if parent_profile == PROFILE_KEYSIGHT_CHASSIS:
            live_labels = live_port_labels_by_node.get(node_id)
            if live_labels and port_label not in live_labels:
                if not _labels_are_port_placeholders(list(live_labels)):
                    continue
        port_profile, metrics = _port_profile_for_parent(parent_profile)
        catalog[key] = {
            'profile': port_profile,
            'label': port_label,
            'parent': parent_key,
            'metrics': metrics,
            'node_id': node_id,
        }
    return catalog


def enrich_catalog_with_observed_keys(
    catalog: Dict[str, Dict[str, Any]],
    topo_id: int,
    *,
    recent_hours: int = 48,
) -> Dict[str, Dict[str, Any]]:
    """Add port/device keys present in recent rollups/samples/events.

    Never DISTINCT the full LabMetricSample table — that walks a multi-GB
    timeseries store and wedges Lab Pulse.
    """
    from connect.models import LabMetricRollup, LabMetricSample, LabResourceEvent

    observed: Set[str] = set()
    recent = timezone.now() - timedelta(hours=max(1, int(recent_hours)))
    rollup_keys = list(
        LabMetricRollup.objects.using(TS_DB)
        .filter(topology_id=topo_id, bucket_start__gte=recent)
        .values_list('resource_key', flat=True)
        .distinct()
    )
    observed.update(rollup_keys)
    if not rollup_keys:
        observed.update(
            LabMetricSample.objects.using(TS_DB)
            .filter(topology_id=topo_id, sampled_at__gte=recent)
            .values_list('resource_key', flat=True)
            .distinct()
        )
        observed.update(
            LabResourceEvent.objects.using(TS_DB)
            .filter(topology_id=topo_id, started_at__gte=recent)
            .values_list('resource_key', flat=True)
            .distinct()
        )
    return apply_observed_keys_to_catalog(catalog, observed)


def _port_profile_for_parent(parent_profile: Optional[str]) -> tuple[str, List[str]]:
    if parent_profile == PROFILE_OCS:
        return 'ocs_port', list(PORT_METRICS)
    if parent_profile in (PROFILE_ARISTA_SWITCH, PROFILE_SONIC_SWITCH):
        return 'switch_port', ['bps_in', 'bps_out', 'input_discards']
    return PROFILE_KEYSIGHT_PORT, list(PORT_METRICS)


def catalog_resource_keys(catalog: Dict[str, Dict[str, Any]]) -> Set[str]:
    """Set of resource keys in a catalog (for ``restrict_*`` filters)."""
    return set(catalog.keys())


def restrict_metric_buckets(
    metric_buckets: Dict[str, Any],
    allowed_keys: Iterable[str],
) -> Dict[str, Any]:
    """Drop metric series not present in this topology's resource catalog."""
    allowed = set(allowed_keys)
    if not allowed:
        return {}
    return {k: v for k, v in (metric_buckets or {}).items() if k in allowed}


def restrict_events_grouped(
    events: Dict[str, Any],
    allowed_keys: Iterable[str],
) -> Dict[str, Any]:
    """Drop grouped timeline events for resources outside this topology."""
    allowed = set(allowed_keys)
    if not allowed:
        return {}
    return {k: v for k, v in (events or {}).items() if k in allowed}


def _port_labels_from_extra(node: LabTopologyNode) -> List[str]:
    """Port labels saved on the topology node (designer slot_layout, ports, etc.)."""
    extra = node.extra or {}
    seen: Set[str] = set()
    out: List[str] = []

    def add(label: Any) -> None:
        s = str(label or '').strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    for p in extra.get('ports') or []:
        add(p if isinstance(p, str) else (p.get('name') or p.get('label')))
    for pd in extra.get('port_details') or []:
        if isinstance(pd, dict):
            add(pd.get('name') or pd.get('label'))
    slot_layout = extra.get('slot_layout')
    slots = slot_layout.get('slots') if isinstance(slot_layout, dict) else slot_layout
    if isinstance(slots, list):
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            for rg in slot.get('resource_groups') or []:
                for p in rg.get('ports') or []:
                    if isinstance(p, dict):
                        add(p.get('name') or p.get('label'))
            for p in slot.get('ports') or []:
                if isinstance(p, dict):
                    add(p.get('name') or p.get('label'))
    for fp in extra.get('flat_ports') or []:
        if isinstance(fp, dict):
            add(fp.get('name') or fp.get('label'))
    for op in extra.get('ocs_physical_ports') or []:
        if isinstance(op, dict):
            add(op.get('label') or op.get('name') or op.get('path_id'))
    return out


def _live_chassis_port_labels(chassis_id: int) -> List[str]:
    """Port labels from live driver (matches metric collector keys like 1.1, 6.2)."""
    cache_key = f'catalog_chassis_ports:{chassis_id}'
    cached = cache_get(cache_key)
    if cached is not None:
        return list(cached)

    labels: List[str] = []
    try:
        from connect.metric_collectors import _chassis_port_label
        from connect.keysight_drivers import get_driver

        chassis = KeysightChassis.objects.filter(pk=chassis_id).first()
        if not chassis:
            cache_set(cache_key, [], 120)
            return []
        driver = get_driver(chassis)
        res = driver.get_ports()
        if res.success and isinstance(res.data, list):
            seen: Set[str] = set()
            for port in res.data:
                if not isinstance(port, dict):
                    continue
                lbl = _chassis_port_label(port).strip()
                if lbl and lbl not in seen:
                    seen.add(lbl)
                    labels.append(lbl)
    except Exception:
        labels = []

    cache_set(cache_key, labels, 300)
    return labels


def _labels_are_port_placeholders(labels: List[str]) -> bool:
    """True when designer saved generic port_1..port_N instead of fabric labels."""
    if not labels:
        return False
    return all(re.fullmatch(r'port_\d+', str(lb).strip()) for lb in labels)


def _observed_port_labels_for_node(topo_id: int, node_id: int) -> List[str]:
    """Port suffixes seen in np_timeseries for this topology node."""
    from connect.models import LabMetricSample

    prefix = f'node_{node_id}__'
    keys = (
        LabMetricSample.objects.using(TS_DB)
        .filter(topology_id=topo_id, resource_key__startswith=prefix)
        .values_list('resource_key', flat=True)
        .distinct()
    )
    labels: List[str] = []
    seen: Set[str] = set()
    for key in keys:
        suffix = key.split('__', 1)[-1]
        if suffix and suffix not in seen:
            seen.add(suffix)
            labels.append(suffix)
    return sorted(labels, key=lambda s: (len(s), s))


def _chassis_port_labels(node: LabTopologyNode, *, live_ports: bool = True) -> List[str]:
    extra = node.extra or {}
    chassis_id = extra.get('chassis_id')
    topo_id = node.topology_id
    if live_ports and chassis_id:
        live = _live_chassis_port_labels(int(chassis_id))
        if live:
            return live
    from_extra = _port_labels_from_extra(node)
    if topo_id and (not live_ports or _labels_are_port_placeholders(from_extra)):
        observed = _observed_port_labels_for_node(topo_id, node.pk)
        if observed:
            return observed
    if from_extra and not _labels_are_port_placeholders(from_extra):
        return from_extra
    if from_extra:
        return from_extra
    if chassis_id:
        chassis = KeysightChassis.objects.filter(pk=chassis_id).first()
        port_count = getattr(chassis, 'port_count', None) if chassis else None
        if port_count:
            return [f'1.{i}' for i in range(1, min(int(port_count) + 1, 33))]
    return []


def _switch_port_labels(node: LabTopologyNode) -> List[str]:
    return _port_labels_from_extra(node)


def _ocs_port_labels(node: LabTopologyNode) -> List[str]:
    return _port_labels_from_extra(node)


def _switch_interface_names_for_port_number(port_number: int, vendor: str = '') -> List[str]:
    """Likely counter names from Arista/SONiC drivers for a physical port index."""
    pn = int(port_number)
    names: List[str] = [f'Ethernet{pn}', f'port_{pn}']
    if (vendor or '').lower() in ('arista', 'sonic', ''):
        block = (pn - 1) // 8 + 1
        off = (pn - 1) % 8 + 1
        names.append(f'Ethernet{block}/{off}')
    seen: Set[str] = set()
    out: List[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _iter_switch_port_detail_rows(node: LabTopologyNode):
    """Yield designer port dicts that carry fabric labels and physical port numbers."""
    extra = node.extra or {}

    def yield_rows(rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if isinstance(row, dict):
                yield row

    for row in yield_rows(extra.get('port_details')):
        yield row
    slot_layout = extra.get('slot_layout')
    slots = slot_layout.get('slots') if isinstance(slot_layout, dict) else slot_layout
    if isinstance(slots, list):
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            for rg in slot.get('resource_groups') or []:
                yield from yield_rows((rg or {}).get('ports'))
            yield from yield_rows(slot.get('ports'))
    for row in yield_rows(extra.get('flat_ports')):
        yield row


def build_switch_counter_aliases(node: LabTopologyNode) -> Dict[str, List[str]]:
    """Map switch counter interface names (Ethernet3/1) to fabric labels (2.1.1)."""
    vendor = (node.device.vendor_type or '').lower() if node.device else ''
    labels_by_port: Dict[int, List[str]] = {}

    for pd in _iter_switch_port_detail_rows(node):
        label = str(pd.get('name') or pd.get('label') or '').strip()
        pn = pd.get('port_number')
        if not label or pn is None:
            continue
        try:
            port_num = int(pn)
        except (TypeError, ValueError):
            continue
        bucket = labels_by_port.setdefault(port_num, [])
        if label not in bucket:
            bucket.append(label)

    aliases: Dict[str, List[str]] = {}
    for port_num, labels in labels_by_port.items():
        if not labels:
            continue
        for iface in _switch_interface_names_for_port_number(port_num, vendor):
            aliases[iface] = list(labels)

    # Designer often stores port_N placeholders without port_details — map counters anyway.
    extra = node.extra or {}
    for raw in extra.get('ports') or []:
        label = str(raw or '').strip()
        m = re.fullmatch(r'port_(\d+)', label)
        if not m:
            continue
        port_num = int(m.group(1))
        for iface in _switch_interface_names_for_port_number(port_num, vendor):
            bucket = aliases.setdefault(iface, [])
            if label not in bucket:
                bucket.append(label)
    return aliases


def alias_switch_metric_buckets(
    metric_buckets: Dict[str, Dict[str, List[List[Any]]]],
    topo_id: int,
) -> Dict[str, Dict[str, List[List[Any]]]]:
    """Copy switch counter series onto designer fabric port keys (e.g. 2.1.1)."""
    out: Dict[str, Dict[str, List[List[Any]]]] = dict(metric_buckets or {})
    nodes = (
        LabTopologyNode.objects.filter(topology_id=topo_id, node_type='switch')
        .select_related('device')
    )
    for node in nodes:
        aliases = build_switch_counter_aliases(node)
        if not aliases:
            continue
        prefix = f'node_{node.pk}__'
        for iface, labels in aliases.items():
            src = out.get(prefix + iface)
            if not src:
                continue
            for label in labels:
                dst_key = prefix + label
                dst = out.setdefault(dst_key, {})
                for metric, series in src.items():
                    if series and not dst.get(metric):
                        dst[metric] = list(series)
    return out
