"""Lab Port Usage graph (schema ``lab_port_usage_v1``).

Builds the usage-graph JSON the Lab Pulse pages render: the topology graph from
``TopologyGraphBuilder``, reservation overlays, and 31-day duty cycle per port from
``port_usage.aggregate_usage_for_topology``. Output is a dict with ``schema_version``,
``graph_kind``, nodes, and edges colored by duty. No device I/O.
See ``docs/development/subsystems/metrics-insights.md``.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from django.utils import timezone as django_tz

from .port_usage import USAGE_WINDOW_DAYS, aggregate_usage_for_topology, resource_key_for_port
from .topology_graph import TopologyGraphBuilder, _iso_now


SCHEMA_VERSION = 1
GRAPH_KIND = 'lab_port_usage_v1'

_FABRIC_SLOT_PORT = re.compile(r'__s(\d+)__(\d+)$')


def _duty_color(duty: float) -> str:
    """Heat color for utilization duty cycle (0-1)."""
    if duty >= 0.75:
        return '#ef4444'
    if duty >= 0.5:
        return '#f59e0b'
    if duty >= 0.25:
        return '#3b82f6'
    if duty > 0.05:
        return '#22c55e'
    return '#64748b'


def _iter_device_ports(dev: dict):
    """Yield every port dict on a fabric device (OCS shelves + port groups)."""
    for shelf in dev.get('ocs_shelves') or []:
        for bank in shelf.get('banks') or []:
            for p in bank.get('ports') or []:
                yield p
    for pg in dev.get('port_groups') or []:
        for p in pg.get('ports') or []:
            yield p


def _metric_label_from_port(port: dict) -> str:
    """Label aligned with collectors / timeline (slot.port when possible)."""
    from connect.topology_resource_catalog import chassis_port_metric_label

    label = chassis_port_metric_label(port)
    cn = port.get('card_number') if port.get('card_number') is not None else port.get('slot')
    if cn is not None:
        return label

    disp = (port.get('port_display') or '').strip()
    pid = (port.get('id') or '').strip()
    m = _FABRIC_SLOT_PORT.search(pid)
    if m:
        slot = int(m.group(1))
        idx = int(m.group(2))
        if disp and '.' in disp:
            return label
        if disp:
            return f'{slot}.{disp}'
        return f'{slot}.{idx}'
    return label


def _port_resource_key(dev_id: str, port: dict) -> str:
    """Canonical key for timeline, usage DB, and cross-hover (not fabric id)."""
    label = _metric_label_from_port(port)
    if label:
        return resource_key_for_port(dev_id, label)
    pid = (port.get('id') or '').strip()
    return pid or dev_id


def _usage_lookup_keys(dev_id: str, port: dict) -> List[str]:
    keys: List[str] = []
    rkey = _port_resource_key(dev_id, port)
    if rkey:
        keys.append(rkey)
    pid = (port.get('id') or '').strip()
    if pid and pid not in keys:
        keys.append(pid)
    for lbl in (port.get('label'), port.get('name'), port.get('port_display')):
        if not lbl:
            continue
        alt = resource_key_for_port(dev_id, str(lbl).strip())
        if alt not in keys:
            keys.append(alt)
    return keys


def _usage_for_port(usage_by_key: dict, dev_id: str, port: dict) -> tuple[str, dict]:
    """Resolve utilization; return canonical metric resource_key."""
    keys = _usage_lookup_keys(dev_id, port)
    rkey = keys[0] if keys else dev_id
    for k in keys:
        u = usage_by_key.get(k)
        if u:
            return rkey, u
    return rkey, {}


def _resolve_connection_port_key(
    dev_id: str,
    port_ref: str,
    fabric_to_metric: Dict[str, str],
) -> str:
    if not port_ref:
        return ''
    if port_ref in fabric_to_metric:
        return fabric_to_metric[port_ref]
    if dev_id and port_ref.startswith(f'{dev_id}__'):
        return port_ref
    if '__' in port_ref and port_ref.count('__') >= 2:
        if port_ref in fabric_to_metric:
            return fabric_to_metric[port_ref]
        m = _FABRIC_SLOT_PORT.search(port_ref)
        if m and dev_id:
            return resource_key_for_port(dev_id, f'{m.group(1)}.{m.group(2)}')
        return port_ref
    return resource_key_for_port(dev_id, port_ref) if dev_id else port_ref


def _fabric_connection_resource_keys(
    conn: dict,
    fabric_to_metric: Optional[Dict[str, str]] = None,
) -> tuple[str, str, str, str, str, str]:
    """Map port-fabric connection to device/port refs and metric resource keys."""
    sa = (conn.get('src_device') or conn.get('source_device') or '').strip()
    sb = (conn.get('dst_device') or conn.get('target_device') or '').strip()
    pa = (conn.get('src_port') or conn.get('source_port') or conn.get('port_a') or '').strip()
    pb = (conn.get('dst_port') or conn.get('target_port') or conn.get('port_b') or '').strip()
    fmap = fabric_to_metric or {}
    rka = _resolve_connection_port_key(sa, pa, fmap) if fmap else _legacy_conn_key(sa, pa)
    rkb = _resolve_connection_port_key(sb, pb, fmap) if fmap else _legacy_conn_key(sb, pb)
    return sa, sb, pa, pb, rka, rkb


def _legacy_conn_key(dev_id: str, port_ref: str) -> str:
    if not port_ref:
        return ''
    if '__' in port_ref:
        return port_ref
    return resource_key_for_port(dev_id, port_ref) if dev_id else ''


def _port_utilization_entry(p: dict, dev: dict, u: dict) -> dict:
    duty = float(u.get('duty_cycle') or 0.0)
    reserved = bool(p.get('reserved') or dev.get('reserved'))
    in_use = bool(u.get('in_use'))
    entry = {
        'duty_cycle_31d': round(duty, 4),
        'heatmap_31d': u.get('heatmap_31d') or [0.0] * USAGE_WINDOW_DAYS,
        'episodes_31d': int(u.get('episodes_31d') or 0),
        'in_use': in_use,
        'reserved': reserved,
        'color': _duty_color(duty if not in_use else max(duty, 0.85)),
    }
    if u.get('last_team'):
        entry['last_team'] = u['last_team']
    if u.get('last_user'):
        entry['last_user'] = u['last_user']
    return entry


def _enrich_port_copy(
    dev_id: str,
    port: dict,
    dev: dict,
    usage_by_key: dict,
    fabric_to_metric: Dict[str, str],
) -> Tuple[dict, dict]:
    """Return (flat port summary, enriched fabric port dict)."""
    rkey, u = _usage_for_port(usage_by_key, dev_id, port)
    fid = (port.get('id') or '').strip()
    if fid:
        fabric_to_metric[fid] = rkey
    util = _port_utilization_entry(port, dev, u)
    pname = _metric_label_from_port(port) or port.get('label') or port.get('name') or ''
    flat = {
        'resource_key': rkey,
        'fabric_port_id': fid,
        'label': pname,
        'role': port.get('role', ''),
        'status': port.get('status', port.get('health', 'unknown')),
        'peer': port.get('peer', '') or port.get('peer_port_label', ''),
        'ocs_triplet': port.get('ocs_triplet', ''),
        'port_display': port.get('port_display') or pname,
        'utilization': util,
    }
    enriched = {**port, 'resource_key': rkey, 'utilization': util}
    return flat, enriched


def _enrich_port_groups(
    dev_id: str,
    dev: dict,
    usage_by_key: dict,
    fabric_to_metric: Dict[str, str],
) -> List[dict]:
    groups: List[dict] = []
    for pg in dev.get('port_groups') or []:
        ports_enriched = []
        for p in pg.get('ports') or []:
            _, enriched = _enrich_port_copy(dev_id, p, dev, usage_by_key, fabric_to_metric)
            ports_enriched.append(enriched)
        groups.append({**pg, 'ports': ports_enriched})
    return groups


def _enrich_ocs_shelves(
    dev_id: str,
    dev: dict,
    usage_by_key: dict,
    fabric_to_metric: Dict[str, str],
) -> List[dict]:
    shelves: List[dict] = []
    for shelf in dev.get('ocs_shelves') or []:
        banks_out = []
        for bank in shelf.get('banks') or []:
            ports_enriched = []
            for p in bank.get('ports') or []:
                _, enriched = _enrich_port_copy(dev_id, p, dev, usage_by_key, fabric_to_metric)
                ports_enriched.append(enriched)
            banks_out.append({**bank, 'ports': ports_enriched})
        shelves.append({**shelf, 'banks': banks_out})
    return shelves


class LabPortUsageGraphBuilder:
    """Merge NormalizedGraph v1, port-fabric ports, reservations, and usage cycles."""

    def __init__(self, topo_id: int):
        self.topo_id = topo_id

    def build(
        self,
        *,
        live: bool = True,
        lldp: bool = True,
        ocs: bool = True,
        planned: bool = True,
        reservations: bool = True,
    ) -> dict:
        """Return the ``lab_port_usage_v1`` payload (devices, port_nodes, port_links, usage_summary, meta).

        ``live`` / ``lldp`` / ``ocs`` are passed to the topology graph and port-fabric
        builders; ``live=True`` may probe devices.
        """
        from .lab_topology_views import _build_port_fabric_payload
        from .models import LabTopology

        topo = LabTopology.objects.get(pk=self.topo_id)
        now = django_tz.now()
        window_start = now - timedelta(days=USAGE_WINDOW_DAYS)

        graph_builder = TopologyGraphBuilder(topo_id=self.topo_id)
        base = graph_builder.build(
            live=live,
            lldp=lldp,
            ocs=ocs,
            planned=planned,
            reservations=reservations,
            use_cache=True,
        )

        fabric, _timing = _build_port_fabric_payload(
            topo,
            want_live=live,
            want_lldp=lldp,
            force_refresh=False,
            profile=False,
        )

        usage_by_key = aggregate_usage_for_topology(self.topo_id, now=now)
        fabric_to_metric: Dict[str, str] = {}

        devices_out: List[dict] = []
        port_nodes: List[dict] = []
        port_links: List[dict] = []
        topology_resource_keys: set[str] = set()

        for dev in fabric.get('devices') or []:
            dev_id = dev.get('id', '')
            topology_resource_keys.add(dev_id)
            dev_usage_seconds = 0.0
            ports_out: List[dict] = []

            for p in _iter_device_ports(dev):
                flat, _enriched = _enrich_port_copy(
                    dev_id, p, dev, usage_by_key, fabric_to_metric,
                )
                topology_resource_keys.add(flat['resource_key'])
                duty = flat['utilization']['duty_cycle_31d']
                ports_out.append(flat)
                dev_usage_seconds += duty
                port_nodes.append({
                    'id': f'port:{flat["resource_key"]}',
                    'kind': 'port',
                    'parent_device': dev_id,
                    'label': flat['label'],
                    'duty_cycle_31d': duty,
                    'color': flat['utilization']['color'],
                    'in_use': flat['utilization']['in_use'],
                    'reserved': flat['utilization']['reserved'],
                })

            n_ports = max(1, len(ports_out))
            devices_out.append({
                'id': dev_id,
                'label': dev.get('label', ''),
                'ip': dev.get('ip', ''),
                'node_type': dev.get('node_type', 'generic'),
                'vendor': dev.get('vendor', ''),
                'chassis_type': dev.get('chassis_type', ''),
                'status': dev.get('status', 'unknown'),
                'reserved': bool(dev.get('reserved')),
                'reservation': dev.get('reservation'),
                'port_groups': _enrich_port_groups(dev_id, dev, usage_by_key, fabric_to_metric),
                'ocs_shelves': _enrich_ocs_shelves(dev_id, dev, usage_by_key, fabric_to_metric),
                'ports': ports_out,
                'utilization': {
                    'duty_cycle_31d': round(dev_usage_seconds / n_ports, 4),
                    'ports_total': len(ports_out),
                    'ports_in_use': sum(1 for p in ports_out if p['utilization']['in_use']),
                },
            })

        port_node_ids = {n['id'] for n in port_nodes}
        for conn in fabric.get('connections') or []:
            sa, sb, pa, pb, rka, rkb = _fabric_connection_resource_keys(conn, fabric_to_metric)
            if not rka or not rkb:
                continue
            src_id = f'port:{rka}'
            tgt_id = f'port:{rkb}'
            if src_id not in port_node_ids or tgt_id not in port_node_ids:
                continue
            ua = usage_by_key.get(rka, {})
            ub = usage_by_key.get(rkb, {})
            duty = max(float(ua.get('duty_cycle') or 0), float(ub.get('duty_cycle') or 0))
            port_links.append({
                'id': conn.get('id') or f'{rka}__{rkb}',
                'source': src_id,
                'target': tgt_id,
                'source_device': sa,
                'target_device': sb,
                'port_a': pa,
                'port_b': pb,
                'resource_key_a': rka,
                'resource_key_b': rkb,
                'type': conn.get('type', 'unknown'),
                'health': conn.get('health', 'unknown'),
                'duty_cycle_31d': round(duty, 4),
                'color': _duty_color(duty),
            })

        fabric_node_ids = {d['id'] for d in devices_out}
        nodes_enriched = []
        for n in base.get('nodes') or []:
            nid = n.get('id', '')
            if nid not in fabric_node_ids:
                continue
            node_copy = dict(n)
            duties = [
                float(u.get('duty_cycle') or 0)
                for k, u in usage_by_key.items()
                if k in topology_resource_keys and k.startswith(f'{nid}__')
            ]
            node_copy['utilization'] = {
                'duty_cycle_31d': round(sum(duties) / max(1, len(duties)), 4) if duties else 0.0,
                'ports_tracked': len(duties),
            }
            if n.get('reservation'):
                node_copy['utilization']['reserved'] = True
            nodes_enriched.append(node_copy)

        scoped_usage = {
            k: v for k, v in usage_by_key.items() if k in topology_resource_keys
        }
        stats = dict((base.get('meta') or {}).get('stats') or {})
        stats['usage_ports_tracked'] = len(scoped_usage)
        stats['usage_ports_in_use'] = sum(1 for u in scoped_usage.values() if u.get('in_use'))
        stats['usage_episodes_31d'] = sum(int(u.get('episodes_31d') or 0) for u in scoped_usage.values())

        return {
            'schema_version': SCHEMA_VERSION,
            'graph_kind': GRAPH_KIND,
            'topology_id': self.topo_id,
            'topo_name': base.get('topo_name') or topo.name,
            'meta': {
                'generated_at': _iso_now(),
                'window_days': USAGE_WINDOW_DAYS,
                'window_start': window_start.isoformat(),
                'window_end': now.isoformat(),
                'sources': dict((base.get('meta') or {}).get('sources') or {}),
                'stats': stats,
                'port_fabric_timing_ms': _timing,
            },
            'nodes': nodes_enriched,
            'links': [
                lk for lk in (base.get('links') or [])
                if lk.get('source') in fabric_node_ids and lk.get('target') in fabric_node_ids
            ],
            'conflicts': base.get('conflicts') or [],
            'devices': devices_out,
            'port_nodes': port_nodes,
            'port_links': port_links,
            'usage_summary': {
                'by_resource_key': {
                    k: {
                        'duty_cycle_31d': v.get('duty_cycle'),
                        'heatmap_31d': v.get('heatmap_31d'),
                        'episodes_31d': v.get('episodes_31d'),
                        'in_use': v.get('in_use'),
                    }
                    for k, v in scoped_usage.items()
                },
            },
            'ocs_summary': base.get('ocs_summary') or fabric.get('summary', {}).get('ocs'),
        }
