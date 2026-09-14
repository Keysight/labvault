"""
Canonical topology graph builder (read-only).

Produces NormalizedGraph v1 for graph.json and legacy adapters for fabric.json /
port-fabric.json. Existing view code paths remain when feature flags are off.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from django.utils import timezone as django_tz

from .ip_addressing import display_mgmt_address
from .lldp_persistence import LLDP_RETENTION_SECONDS, load_switch_lldp_by_ip
from .models import Device, KeysightChassis, KeysightReservation, LabTopology, LabTopologyLink, LabTopologyNode
from .topology_ip_index import build_ip_to_node_map

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
CACHE_TTL_SECONDS = 30

_graph_cache: Dict[str, Tuple[dict, float]] = {}
_graph_cache_lock = threading.Lock()

# Theme-aligned link colors (see docs/TOPOLOGY_VIEWS_IMPLEMENTATION_PLAN.md §3.3)
COLOR_UP = '#22c55e'
COLOR_STALE = '#f59e0b'
COLOR_DOWN = '#ef4444'
COLOR_PLANNED = '#64748b'
COLOR_OCS = '#fb923c'
COLOR_CONFLICT = '#ef4444'
COLOR_LLDP_ONLY = '#f59e0b'

SITE_DIR = Path(__file__).resolve().parent.parent / 'resources'


def invalidate_cache(topo_id: int | None = None) -> None:
    with _graph_cache_lock:
        if topo_id is None:
            _graph_cache.clear()
            return
        prefix = f'{topo_id}:'
        for key in list(_graph_cache.keys()):
            if key.startswith(prefix):
                del _graph_cache[key]


def switch_ips_for_topology(topo_id: int) -> List[str]:
    """Mgmt IPs of switch nodes belonging to one topology."""
    ips: Set[str] = set()
    for n in LabTopologyNode.objects.filter(topology_id=topo_id).select_related('device'):
        if n.node_type != 'switch':
            continue
        if n.device_id and n.device and n.device.ip_address:
            ips.add(n.device.ip_address.strip())
        dip = (n.extra or {}).get('device_ip')
        if dip:
            ips.add(str(dip).strip())
    return sorted(ip for ip in ips if ip)


def topology_ids_for_switch_ips(ips: Set[str] | List[str]) -> List[int]:
    """Topology IDs that include any of the given switch IPs."""
    want = {str(i).strip() for i in ips if i}
    if not want:
        return []
    found: Set[int] = set()
    for n in LabTopologyNode.objects.filter(node_type='switch').select_related('device'):
        ip = ''
        if n.device_id and n.device and n.device.ip_address:
            ip = n.device.ip_address.strip()
        if not ip:
            ip = str((n.extra or {}).get('device_ip') or '').strip()
        if ip in want:
            found.add(n.topology_id)
    return sorted(found)


def _cache_get(key: str) -> dict | None:
    with _graph_cache_lock:
        entry = _graph_cache.get(key)
        if entry and time.time() < entry[1]:
            return entry[0]
    return None


def _cache_set(key: str, graph: dict) -> None:
    with _graph_cache_lock:
        _graph_cache[key] = (graph, time.time() + CACHE_TTL_SECONDS)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chassis_name_to_ip_map() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for sf in sorted(SITE_DIR.glob('ocs_photonic_site*.json')):
        try:
            sd = json.loads(sf.read_text())
            for kc in sd.get('keysight_chassis') or []:
                nm = (kc.get('name') or '').lower().replace('_', '').replace('-', '').replace(' ', '')
                if nm and kc.get('ip'):
                    out[nm] = kc['ip']
        except Exception:
            pass
    for kc in KeysightChassis.objects.all():
        for key in (kc.hostname, kc.ip_address):
            if key:
                out[key.lower().replace('_', '').replace('-', '').replace(' ', '')] = kc.ip_address
    return out


def _resolve_chassis_ip(label: str, chassis_map: Dict[str, str]) -> str:
    lbl = label.lower().replace('-', '').replace(' ', '').replace('_', '')
    if lbl in chassis_map:
        return chassis_map[lbl]
    m = re.search(r'(\d+)$', lbl)
    if m:
        num = str(int(m.group(1)))
        base = lbl[: lbl.rfind(m.group(1))]
        for k, v in chassis_map.items():
            kb = k.rstrip('0123456789')
            kn = k[len(kb) :]
            if kn == num and base[:4] == kb[:4]:
                return v
    return ''


class TopologyGraphBuilder:
    """Build NormalizedGraph for one topology or global device map."""

    def __init__(self, topo_id: int | None = None):
        self.topo_id = topo_id
        self._sources_meta: Dict[str, Dict[str, Any]] = {}

    def build(
        self,
        *,
        live: bool = True,
        lldp: bool = True,
        ocs: bool = True,
        planned: bool = True,
        reservations: bool = True,
        use_cache: bool = True,
    ) -> dict:
        cache_key = (
            f'{self.topo_id or "global"}:'
            f'{int(live)}{int(lldp)}{int(ocs)}{int(planned)}{int(reservations)}'
        )
        if use_cache:
            cached = _cache_get(cache_key)
            if cached is not None:
                out = dict(cached)
                out.setdefault('meta', {})['cache_hit'] = True
                return out

        if self.topo_id is None:
            graph = self._build_global(lldp=lldp)
        else:
            graph = self._build_topo(
                self.topo_id,
                live=live,
                lldp=lldp,
                ocs=ocs,
                planned=planned,
                reservations=reservations,
            )
        graph['meta']['cache_hit'] = False
        _cache_set(cache_key, graph)
        return graph

    def get_freshness(self) -> dict:
        return dict(self._sources_meta)

    @staticmethod
    def link_health(lldp_entry: dict | None, now: float | None = None) -> str:
        if not lldp_entry:
            return 'planned'
        now = now if now is not None else time.time()
        up = lldp_entry.get('up', True)
        last_seen = lldp_entry.get('last_seen') or lldp_entry.get('updated_at')
        if last_seen is not None:
            try:
                age = now - float(last_seen)
            except (TypeError, ValueError):
                age = 0
        else:
            age = 0
        if not up or age > LLDP_RETENTION_SECONDS:
            return 'down'
        if age > 900:  # 15 minutes
            return 'stale'
        return 'up'

    @staticmethod
    def link_color(health: str, link_type: str = '') -> str:
        if health == 'conflict':
            return COLOR_CONFLICT
        if link_type in ('ocs_active', 'ocs'):
            return COLOR_OCS if health in ('up', 'active') else COLOR_PLANNED
        if health == 'up' or health == 'active':
            return COLOR_UP
        if health == 'stale' or health == 'lldp_only':
            return COLOR_STALE
        if health == 'down' or health == 'alarm':
            return COLOR_DOWN
        if health == 'planned':
            return COLOR_PLANNED
        return COLOR_PLANNED

    def to_fabric_legacy(self, graph: dict) -> dict:
        """Map NormalizedGraph → fabric.json response shape."""
        nodes = []
        for n in graph.get('nodes') or []:
            nodes.append({
                'id': n['id'],
                'db_pk': n.get('db_pk'),
                'label': n.get('label', ''),
                'ip': n.get('mgmt_display') or n.get('mgmt_ipv4', ''),
                'node_type': n.get('kind', 'generic'),
                'vendor': n.get('vendor', ''),
                'x': n.get('x', 0),
                'y': n.get('y', 0),
                'ports': [p.get('name', p) if isinstance(p, dict) else p for p in (n.get('ports') or [])],
                'status': n.get('status', 'unknown'),
                'device_id': n.get('device_id'),
            })
        links = []
        for lk in graph.get('links') or []:
            h = lk.get('health', 'planned')
            lt = lk.get('type', '')
            color = lk.get('color') or self.link_color(h, lt)
            links.append({
                'id': lk['id'],
                'type': lt if lt != 'lldp' else 'lldp',
                'source': lk['source'],
                'target': lk['target'],
                'port_a': lk.get('port_a', ''),
                'port_b': lk.get('port_b', ''),
                'cable_type': lk.get('cable_type', 'direct'),
                'health': 'lldp_only' if h == 'stale' and lt == 'lldp' else h,
                'color': color,
                'label': lk.get('label', ''),
                'ocs_triplets': lk.get('ocs_triplets') or [],
                'loss_db': lk.get('loss_db'),
                'ocs_path_id': lk.get('ocs_path_id'),
            })
        ocs_src = (graph.get('meta') or {}).get('sources', {}).get('ocs_live', {})
        ocs_summary = graph.get('ocs_summary') or {
            'total': 0,
            'active': 0,
            'alarm': 0,
            'unmapped': 0,
            'error': ocs_src.get('error') or '',
        }
        return {
            'nodes': nodes,
            'links': links,
            'ocs_summary': ocs_summary,
            'fetched_at': (graph.get('meta') or {}).get('generated_at', _iso_now()),
            'topo_id': graph.get('topology_id'),
            'topo_name': graph.get('topo_name', ''),
        }

    def to_port_fabric_legacy(self, graph: dict) -> dict:
        """
        Minimal port-fabric shape from normalized graph (Phase 3 expands).
        Side-by-side validation uses graph/compare.json for port-fabric until full adapter.
        """
        devices = []
        connections = []
        for n in graph.get('nodes') or []:
            ip = n.get('mgmt_display') or n.get('mgmt_ipv4', '')
            ports = []
            for p in n.get('ports') or []:
                pname = p.get('name', p) if isinstance(p, dict) else str(p)
                ports.append({
                    'label': pname,
                    'role': (p.get('role') if isinstance(p, dict) else None) or 'unknown',
                    'status': (p.get('status') if isinstance(p, dict) else None) or 'unknown',
                    'triplets': (p.get('ocs_triplet') if isinstance(p, dict) else None) or [],
                })
            devices.append({
                'id': n['id'],
                'label': n.get('label', ''),
                'ip': ip,
                'node_type': n.get('kind', 'generic'),
                'ports': ports,
            })
        for lk in graph.get('links') or []:
            if not lk.get('port_a') or not lk.get('port_b'):
                continue
            connections.append({
                'source_device': lk['source'],
                'source_port': lk['port_a'],
                'target_device': lk['target'],
                'target_port': lk['port_b'],
                'type': lk.get('type', 'unknown'),
                'health': lk.get('health', 'unknown'),
            })
        return {
            'devices': devices,
            'connections': connections,
            'summary': (graph.get('meta') or {}).get('stats', {}),
            'fetched_at': (graph.get('meta') or {}).get('generated_at', _iso_now()),
            'note': 'port_fabric_legacy_minimal_v1',
        }

    def to_designer_payload(self, graph: dict) -> dict:
        """Superset for Designer LLDP overlay (read-only layer)."""
        return {
            'nodes': [
                {
                    'id': n['id'],
                    'label': n.get('label'),
                    'x': n.get('x'),
                    'y': n.get('y'),
                    'live_health': n.get('status'),
                }
                for n in graph.get('nodes') or []
            ],
            'live_links': [
                {
                    'id': lk['id'],
                    'source': lk['source'],
                    'target': lk['target'],
                    'port_a': lk.get('port_a'),
                    'port_b': lk.get('port_b'),
                    'health': lk.get('health'),
                    'color': lk.get('color'),
                }
                for lk in graph.get('links') or []
                if lk.get('type') != 'planned'
            ],
            'conflicts': graph.get('conflicts') or [],
        }

    def _source_ok(self, name: str, error: str | None = None) -> None:
        self._sources_meta[name] = {
            'ok': error is None,
            'at': _iso_now() if error is None else None,
            'error': error,
        }

    def _build_topo(
        self,
        topo_id: int,
        *,
        live: bool,
        lldp: bool,
        ocs: bool,
        planned: bool,
        reservations: bool,
    ) -> dict:
        self._sources_meta = {}
        topo = LabTopology.objects.filter(pk=topo_id).first()
        if not topo:
            return self._empty_graph(topo_id, name='')

        chassis_map = _chassis_name_to_ip_map()
        nodes: Dict[str, dict] = {}
        raw_links: List[dict] = []
        ocs_summary = {'total': 0, 'active': 0, 'alarm': 0, 'unmapped': 0, 'error': ''}

        if planned:
            nodes, plan_links = self._load_planned(topo, chassis_map)
            raw_links.extend(plan_links)
            self._source_ok('planned')
        else:
            self._source_ok('planned', 'disabled')

        if ocs and live:
            ocs_links, ocs_summary = self._load_ocs_live(topo, nodes)
            raw_links.extend(ocs_links)
        elif ocs:
            self._source_ok('ocs_live', 'live=0')
        else:
            self._source_ok('ocs_live', 'disabled')

        if lldp:
            lldp_links, chassis_links = self._load_lldp_for_topo(topo, nodes, chassis_map)
            raw_links.extend(lldp_links)
            raw_links.extend(chassis_links)
        else:
            for s in ('lldp_db', 'lldp_cache', 'chassis_links', 'switch_files'):
                self._source_ok(s, 'disabled')

        if reservations:
            self._apply_reservations(nodes)
            self._source_ok('reservations')
        else:
            self._source_ok('reservations', 'disabled')

        merged_links, conflicts = self._merge_links(raw_links)
        stats = self._compute_stats(nodes, merged_links, conflicts)

        return {
            'schema_version': SCHEMA_VERSION,
            'topology_id': topo_id,
            'topo_name': topo.name,
            'ocs_summary': ocs_summary,
            'meta': {
                'generated_at': _iso_now(),
                'cache_hit': False,
                'sources': dict(self._sources_meta),
                'stats': stats,
            },
            'nodes': list(nodes.values()),
            'links': merged_links,
            'conflicts': conflicts,
        }

    def _empty_graph(self, topo_id: int, name: str) -> dict:
        return {
            'schema_version': SCHEMA_VERSION,
            'topology_id': topo_id,
            'topo_name': name,
            'ocs_summary': {'total': 0, 'active': 0, 'alarm': 0, 'unmapped': 0, 'error': ''},
            'meta': {'generated_at': _iso_now(), 'cache_hit': False, 'sources': {}, 'stats': {}},
            'nodes': [],
            'links': [],
            'conflicts': [],
        }

    def _load_planned(self, topo: LabTopology, chassis_map: Dict[str, str]) -> Tuple[Dict[str, dict], List[dict]]:
        nodes: Dict[str, dict] = {}
        links: List[dict] = []
        for n in topo.nodes.select_related('device').all():
            ip = n.device.ip_address if n.device else (n.extra or {}).get('device_ip', '')
            ip6 = ''
            if n.device:
                ip6 = getattr(n.device, 'effective_mgmt_ipv6', '') or getattr(
                    n.device, 'mgmt_ipv6', '',
                ) or ''
            else:
                ip6 = (n.extra or {}).get('mgmt_ipv6', '') or ''
            if not ip and n.node_type == 'chassis':
                ip = _resolve_chassis_ip(n.label, chassis_map)
            cid = (n.extra or {}).get('chassis_id')
            if cid and not ip6:
                try:
                    from .models import KeysightChassis
                    ch = KeysightChassis.objects.filter(pk=int(cid)).only(
                        'mgmt_ipv6', 'ip_address',
                    ).first()
                    if ch:
                        ip6 = getattr(ch, 'effective_mgmt_ipv6', '') or ch.mgmt_ipv6 or ''
                        if not ip:
                            ip = ch.ip_address or ip
                except (TypeError, ValueError):
                    pass
            vendor = (n.device.vendor_type if n.device else '') or n.node_type
            pref = (
                n.device.preferred_ip_version if n.device
                else 'auto'
            )
            if cid and not n.device:
                try:
                    from .models import KeysightChassis as _KC
                    _ch = _KC.objects.filter(pk=int(cid)).only(
                        'preferred_ip_version',
                    ).first()
                    if _ch:
                        pref = _ch.preferred_ip_version
                except (TypeError, ValueError):
                    pass
            mgmt_disp = display_mgmt_address(
                ipv4=ip or '',
                ipv6=ip6 or '',
                preferred=pref,
                hostname=n.label or '',
            )
            nid = f'node_{n.pk}'
            nodes[nid] = {
                'id': nid,
                'db_pk': n.pk,
                'kind': n.node_type or 'generic',
                'label': n.label,
                'mgmt_ipv4': ip or '',
                'mgmt_ipv6': ip6 or '',
                'mgmt_display': mgmt_disp or ip or '',
                'device_id': n.device_id,
                'chassis_id': (n.extra or {}).get('chassis_id'),
                'status': 'unknown',
                'vendor': vendor,
                'vendor_secondary': '',
                'dual_os': bool((n.extra or {}).get('dual_os')),
                'model': (n.device.model_name if n.device else '') or '',
                'serial': '',
                'site': '',
                'rack': '',
                'uptime_s': None,
                'x': n.x,
                'y': n.y,
                'ports': list((n.extra or {}).get('ports') or []),
                'reservation': None,
                'team_tags': list((n.extra or {}).get('team_tags') or []),
                'badges': [],
                'detail_url': f'/devices/{n.device_id}/' if n.device_id else '',
            }
        for lk in topo.links.select_related('node_a', 'node_b').all():
            na_id = f'node_{lk.node_a_id}'
            nb_id = f'node_{lk.node_b_id}'
            links.append({
                'id': f'plan_{lk.pk}',
                'type': 'planned',
                'source': na_id,
                'target': nb_id,
                'port_a': lk.port_a or '',
                'port_b': lk.port_b or '',
                'health': 'planned',
                'provenance': ['planned'],
                'last_seen': None,
                'cable_type': lk.cable_type or 'dac',
                'ocs_triplets': [],
                'color': COLOR_PLANNED,
                'aggregate_key': '|'.join(sorted((na_id, nb_id))),
                'parallel_count': 1,
                'label': lk.label or lk.cable_type or '',
                '_priority': 6,
            })
        return nodes, links

    def _load_ocs_live(self, topo: LabTopology, nodes: Dict[str, dict]) -> Tuple[List[dict], dict]:
        ocs_summary = {'total': 0, 'active': 0, 'alarm': 0, 'unmapped': 0, 'error': ''}
        links: List[dict] = []
        ocs_node = topo.nodes.filter(node_type='ocs').select_related('device').first()
        if not ocs_node or not ocs_node.device:
            self._source_ok('ocs_live', 'no OCS node')
            return links, ocs_summary

        def _fetch():
            from . import ocs_helpers
            from .drivers import get_driver

            ocs_dev = ocs_node.device
            drv = get_driver(ocs_dev)
            rows = drv.fetch_crossconnect_list()
            xcons = (drv.get_ocs_crossconnects(raw_rows=rows).data or [])
            triplet_map = ocs_helpers.build_ocs_triplet_map(ocs_dev.ip_address, Device.objects.all())
            return xcons, triplet_map

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_fetch)
                # OCS fetch is typically 4–6s; legacy fabric has no cap.
                xcons, triplet_map = fut.result(timeout=15.0)
        except concurrent.futures.TimeoutError:
            self._source_ok('ocs_live', 'Timeout after 15s')
            ocs_summary['error'] = 'Timeout after 15s'
            return links, ocs_summary
        except Exception as exc:
            logger.warning('topology_graph OCS fetch failed: %s', exc)
            self._source_ok('ocs_live', str(exc))
            ocs_summary['error'] = str(exc)
            return links, ocs_summary

        self._source_ok('ocs_live')
        ip_to_node = build_ip_to_node_map(nodes)
        devid_to_node = {v['device_id']: v['id'] for v in nodes.values() if v.get('device_id')}

        def resolve_end(endpoint_info):
            if not endpoint_info:
                return None
            did = endpoint_info.get('device_id')
            if did and did in devid_to_node:
                return devid_to_node[did]
            eip = endpoint_info.get('ip', '')
            return ip_to_node.get(eip)

        from . import ocs_helpers

        _ALARM_CODES = {'CR', 'MJ', 'MN'}
        ocs_summary['total'] = len(xcons)
        for xc in xcons:
            ta = ocs_helpers.norm_ocs_triplet_key(xc.get('port_a', ''))
            tb = ocs_helpers.norm_ocs_triplet_key(xc.get('port_b', ''))
            ea = triplet_map.get(ta)
            eb = triplet_map.get(tb)
            h2 = xc.get('h2', {})
            alarm = (h2.get('alarm', 'CL') in _ALARM_CODES) or (h2.get('oc', 'OK') not in ('OK', ''))
            loss = h2.get('loss')
            if alarm:
                health, color = 'alarm', COLOR_DOWN
                ocs_summary['alarm'] += 1
            else:
                health, color = 'active', COLOR_UP
                ocs_summary['active'] += 1
            src_id = resolve_end(ea)
            dst_id = resolve_end(eb)
            if not src_id or not dst_id:
                ocs_summary['unmapped'] += 1
                ocs_nid = f'node_{ocs_node.pk}'
                src_id = src_id or ocs_nid
                dst_id = dst_id or ocs_nid
            links.append({
                'id': f'ocs_{xc.get("name", ta + "_" + tb)}',
                'type': 'ocs_active',
                'source': src_id,
                'target': dst_id,
                'port_a': (ea or {}).get('sw_port', ''),
                'port_b': (eb or {}).get('sw_port', ''),
                'health': health,
                'provenance': ['ocs_live'],
                'last_seen': _iso_now(),
                'cable_type': 'optic',
                'ocs_triplets': [ta, tb],
                'color': color,
                'loss_db': loss,
                'ocs_path_id': xc.get('name', ''),
                'aggregate_key': '|'.join(sorted((src_id, dst_id))),
                'parallel_count': 1,
                'label': xc.get('name', ''),
                '_priority': 5,
            })
        return links, ocs_summary

    def _load_lldp_for_topo(
        self,
        topo: LabTopology,
        nodes: Dict[str, dict],
        chassis_map: Dict[str, str],
    ) -> Tuple[List[dict], List[dict]]:
        """Return (lldp_links, chassis_device_links). Records separate source metadata."""
        lldp_links: List[dict] = []
        chassis_links: List[dict] = []

        ip_to_node = build_ip_to_node_map(nodes)
        devid_to_node = {v['device_id']: v['id'] for v in nodes.values() if v.get('device_id')}
        chassisid_to_node: Dict[int, str] = {}
        for n in topo.nodes.all():
            cid = (n.extra or {}).get('chassis_id')
            if cid:
                chassisid_to_node[int(cid)] = f'node_{n.pk}'
            if n.node_type == 'chassis' and n.label:
                ip = (n.extra or {}).get('device_ip') or _resolve_chassis_ip(n.label, chassis_map)
                if ip:
                    ip_to_node[ip] = f'node_{n.pk}'

        # ── 1. LLDP from get_cached_topology() (Device↔Device links) ──────────
        lldp_db_ok = True
        try:
            from .topology import CHASSIS_NODE_PREFIX, get_cached_topology

            lldp_topo = get_cached_topology() or {}

            def _map_global_node(gid: str) -> str:
                if gid.startswith(CHASSIS_NODE_PREFIX):
                    try:
                        ch_id = int(gid[len(CHASSIS_NODE_PREFIX) :])
                    except ValueError:
                        return ''
                    return chassisid_to_node.get(ch_id) or ''
                try:
                    did = int(gid)
                except (TypeError, ValueError):
                    return ''
                dev = Device.objects.filter(pk=did).first()
                if dev and dev.ip_address:
                    return ip_to_node.get(dev.ip_address) or devid_to_node.get(did, '')
                return devid_to_node.get(did, '')

            for e in lldp_topo.get('links') or []:
                sid = _map_global_node(str(e.get('source', '')))
                tid = _map_global_node(str(e.get('target', '')))
                if not sid or not tid or sid == tid:
                    continue
                health = self.link_health({'up': True, 'last_seen': time.time()})
                lldp_links.append({
                    'id': f'lldp_{sid}_{tid}_{len(lldp_links)}',
                    'type': 'lldp',
                    'source': sid,
                    'target': tid,
                    'port_a': e.get('local_port', ''),
                    'port_b': e.get('remote_port', ''),
                    'health': health,
                    'provenance': ['lldp_db'],
                    'last_seen': _iso_now(),
                    'cable_type': 'direct',
                    'ocs_triplets': [],
                    'color': self.link_color(health, 'lldp'),
                    'aggregate_key': '|'.join(sorted((sid, tid))),
                    'parallel_count': 1,
                    'label': e.get('label') or 'LLDP',
                    '_priority': 2,
                })
        except Exception as exc:
            lldp_db_ok = False
            logger.debug('topology_graph LLDP load: %s', exc)
        self._source_ok('lldp_db', None if lldp_db_ok else 'no data')

        # ── 2. ChassisDeviceLink rows (Chassis↔Switch from Arista LLDP tables) ─
        chassis_ok = True
        try:
            from .models import ChassisDeviceLink

            for cdl in ChassisDeviceLink.objects.select_related('chassis', 'device').filter(link_status='up'):
                ch_id = cdl.chassis_id
                dev_id = cdl.device_id
                src_nid = chassisid_to_node.get(ch_id)
                if not src_nid:
                    ch_ip = cdl.chassis.ip_address
                    src_nid = ip_to_node.get(ch_ip)
                dst_nid = devid_to_node.get(dev_id)
                if not dst_nid:
                    dev_ip = cdl.device.ip_address if cdl.device_id else ''
                    dst_nid = ip_to_node.get(dev_ip)
                if not src_nid or not dst_nid or src_nid == dst_nid:
                    continue
                last_seen_ts = cdl.last_seen.timestamp() if cdl.last_seen else time.time()
                health = self.link_health({'up': True, 'last_seen': last_seen_ts})
                chassis_links.append({
                    'id': f'cdl_{cdl.pk}',
                    'type': 'lldp',
                    'source': src_nid,
                    'target': dst_nid,
                    'port_a': cdl.port_chassis or '',
                    'port_b': cdl.port_device or '',
                    'health': health,
                    'provenance': ['chassis_links'],
                    'last_seen': cdl.last_seen.isoformat() if cdl.last_seen else _iso_now(),
                    'cable_type': 'direct',
                    'ocs_triplets': [],
                    'color': self.link_color(health, 'lldp'),
                    'aggregate_key': '|'.join(sorted((src_nid, dst_nid))),
                    'parallel_count': 1,
                    'label': '',
                    '_priority': 3,
                })
        except Exception as exc:
            chassis_ok = False
            logger.debug('topology_graph chassis links: %s', exc)
        self._source_ok('chassis_links', None if chassis_ok else str(exc) if not chassis_ok else None)

        # ── 3. 24h persistent switch LLDP store ────────────────────────────────
        switch_ok = True
        try:
            for _ip, nbr_list in load_switch_lldp_by_ip().items():
                src_nid = ip_to_node.get(_ip)
                if not src_nid:
                    continue
                for nbr in nbr_list:
                    lp = nbr.get('local_port', '')
                    if not lp.startswith('Ethernet'):
                        continue
                    last_ts = nbr.get('updated_at') or time.time()
                    health = self.link_health({'up': True, 'last_seen': last_ts})
                    lldp_links.append({
                        'id': f'sw_{_ip}_{lp}',
                        'type': 'lldp',
                        'source': src_nid,
                        'target': src_nid,  # target resolved later by merge if remote known
                        'port_a': lp,
                        'port_b': nbr.get('remote_port', ''),
                        'health': health,
                        'provenance': ['switch_files'],
                        'last_seen': _iso_now(),
                        'cable_type': 'direct',
                        'ocs_triplets': [],
                        'color': self.link_color(health, 'lldp'),
                        'aggregate_key': f'sw|{_ip}|{lp}',
                        'parallel_count': 1,
                        'label': nbr.get('remote_device', ''),
                        '_priority': 4,
                    })
        except Exception as exc:
            switch_ok = False
            logger.debug('topology_graph switch LLDP: %s', exc)
        self._source_ok('switch_files', None if switch_ok else str(exc) if not switch_ok else None)
        self._source_ok('lldp_cache')

        return lldp_links, chassis_links

    def _apply_reservations(self, nodes: Dict[str, dict]) -> None:
        now = django_tz.now()
        active = KeysightReservation.objects.filter(
            start_time__lte=now,
            end_time__gte=now,
            status__in=('upcoming', 'active'),
        ).prefetch_related('items__chassis', 'user')
        for res in active:
            uname = res.user.username if res.user_id else ''
            until = res.end_time.isoformat() if res.end_time else ''
            for item in res.items.all():
                ch = item.chassis
                if not ch:
                    continue
                ch_ip = ch.ip_address
                ch_id = ch.pk
                for n in nodes.values():
                    if n.get('chassis_id') == ch_id:
                        n['reservation'] = {'team': res.title, 'user': uname, 'until': until}
                        n.setdefault('badges', []).append('reserved')
                    elif ch_ip and (
                        n.get('mgmt_ipv4') == ch_ip
                        or n.get('mgmt_display') == ch.mgmt_display
                        or (ch.mgmt_ipv6 and n.get('mgmt_ipv6') == ch.mgmt_ipv6)
                    ):
                        n['reservation'] = {'team': res.title, 'user': uname, 'until': until}
                        n.setdefault('badges', []).append('reserved')

    def _merge_links(self, raw_links: List[dict]) -> Tuple[List[dict], List[dict]]:
        """Dedup by endpoint pair; lower _priority number = higher precedence."""
        by_pair: Dict[Tuple[str, str], dict] = {}
        for lk in raw_links:
            sa, sb = lk.get('source', ''), lk.get('target', '')
            if not sa or not sb or sa == sb:
                continue
            key = tuple(sorted((sa, sb)))
            prev = by_pair.get(key)
            if prev is None or lk.get('_priority', 99) < prev.get('_priority', 99):
                merged = dict(lk)
                prov = set(prev.get('provenance', []) if prev else [])
                prov.update(lk.get('provenance') or [])
                merged['provenance'] = sorted(prov)
                by_pair[key] = merged
            else:
                prov = set(prev.get('provenance') or [])
                prov.update(lk.get('provenance') or [])
                prev['provenance'] = sorted(prov)

        merged = []
        for lk in by_pair.values():
            lk.pop('_priority', None)
            merged.append(lk)

        planned = [l for l in merged if 'planned' in (l.get('provenance') or [])]
        live = [l for l in merged if l.get('type') in ('lldp', 'ocs_active')]
        conflicts = self._detect_conflicts(planned, live)
        for c in conflicts:
            for lid in c.get('link_ids') or []:
                for lk in merged:
                    if lk['id'] == lid:
                        lk['health'] = 'conflict'
                        lk['color'] = COLOR_CONFLICT
        return merged, conflicts

    @staticmethod
    def _detect_conflicts(planned_links: List[dict], live_links: List[dict]) -> List[dict]:
        conflicts = []
        for plan in planned_links:
            for live in live_links:
                if plan['source'] != live['source'] and plan['source'] != live['target']:
                    continue
                if plan['target'] not in (live['source'], live['target']):
                    continue
                if (plan.get('port_a'), plan.get('port_b')) != (live.get('port_a'), live.get('port_b')):
                    conflicts.append({
                        'link_ids': [plan['id'], live['id']],
                        'reason': 'planned_port_mismatch',
                        'detail': (
                            f"Planned {plan.get('port_a')}↔{plan.get('port_b')}  "
                            f"Observed {live.get('port_a')}↔{live.get('port_b')}"
                        ),
                        'severity': 'warning',
                    })
        return conflicts

    def _compute_stats(self, nodes: Dict[str, dict], links: List[dict], conflicts: List[dict]) -> dict:
        health_counts = {'up': 0, 'stale': 0, 'down': 0, 'planned': 0}
        for lk in links:
            h = lk.get('health', 'planned')
            if h in health_counts:
                health_counts[h] += 1
            elif h == 'active':
                health_counts['up'] += 1
            elif h in ('lldp_only', 'stale'):
                health_counts['stale'] += 1
        return {
            'nodes_total': len(nodes),
            'nodes_online': sum(1 for n in nodes.values() if n.get('status') == 'online'),
            'nodes_offline': sum(1 for n in nodes.values() if n.get('status') == 'offline'),
            'nodes_maintenance': sum(1 for n in nodes.values() if n.get('status') == 'maintenance'),
            'links_total': len(links),
            'links_live': health_counts['up'],
            'links_stale': health_counts['stale'],
            'links_planned_only': health_counts['planned'],
            'links_down': health_counts['down'],
            'conflicts': len(conflicts),
        }

    def _build_global(self, *, lldp: bool) -> dict:
        """Network Topology page payload — must match get_cached_topology() shape for D3."""
        from .topology import get_cached_topology

        nodes = []
        links = []
        try:
            topo = get_cached_topology() or {}
            for n in topo.get('nodes') or []:
                nid = n.get('id', '')
                nodes.append({
                    'id': nid,
                    'ip': n.get('ip', ''),
                    'name': n.get('name') or n.get('label') or n.get('ip', '') or str(nid),
                    'vendor': n.get('vendor') or (
                        'keysight' if str(nid).startswith('chassis-') else 'unknown'
                    ),
                    'status': n.get('status', 'unknown'),
                    'model': n.get('model', ''),
                    'color': n.get('color', '#607D8B'),
                    'icon': n.get('icon', 'fa-server'),
                    'tags': list(n.get('tags') or []),
                })
            for e in topo.get('links') or []:
                st = e.get('status', 'up')
                links.append({
                    'source': e.get('source', ''),
                    'target': e.get('target', ''),
                    'local_port': e.get('local_port', ''),
                    'remote_port': e.get('remote_port', ''),
                    'lag': e.get('lag', ''),
                    'speed': e.get('speed', ''),
                    'status': st,
                    'link_count': e.get('link_count', 1),
                })
            self._source_ok('lldp_db')
        except Exception as exc:
            self._source_ok('lldp_db', str(exc))
        return {
            'schema_version': SCHEMA_VERSION,
            'topology_id': None,
            'topo_name': 'global',
            'meta': {
                'generated_at': _iso_now(),
                'cache_hit': False,
                'sources': dict(self._sources_meta),
                'stats': {'nodes_total': len(nodes), 'links_total': len(links)},
            },
            'nodes': nodes,
            'links': links,
            'conflicts': [],
        }


def build_laas_manifest(topo_id: int) -> dict:
    """LaaS TopologyManifest export is hard-dumped from the customer SKU."""
    return {'error': 'not_available', 'topology_id': topo_id}


def build_global_graph(**kwargs) -> dict:
    return TopologyGraphBuilder(topo_id=None).build(planned=False, **kwargs)


def validate_topology_payload(topo_id: int, payload: dict) -> dict:
    """Validate designer import/layout payload; returns ok, conflicts, warnings."""
    warnings: List[dict] = []
    conflicts: List[dict] = []
    nodes = payload.get('nodes') or []
    links = payload.get('links') or []
    seen_ports: Set[str] = set()
    for lk in links:
        pa = (lk.get('port_a') or '').strip()
        pb = (lk.get('port_b') or '').strip()
        if pa and pb:
            key = f"{lk.get('node_a_id')}|{pa}"
            if key in seen_ports:
                warnings.append({'reason': 'duplicate_port', 'detail': key})
            seen_ports.add(key)
    builder = TopologyGraphBuilder(topo_id=topo_id)
    graph = builder.build(live=False, lldp=True, ocs=False, planned=True)
    conflicts.extend(graph.get('conflicts') or [])
    return {'ok': len([c for c in conflicts if c.get('severity') == 'error']) == 0, 'conflicts': conflicts, 'warnings': warnings}


def compare_fabric_legacy_vs_graph(topo_id: int, live: bool = True, lldp: bool = True) -> dict:
    """Side-by-side comparison for iterative validation (no flag flip required)."""
    # Call legacy fabric API view with a minimally-patched request that passes @login_required
    try:
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from . import lab_topology_views

        rf = RequestFactory()
        q = {'live': '1' if live else '0', 'lldp': '1' if lldp else '0'}
        req = rf.get('/lab-topology/%d/fabric.json' % topo_id, q)
        # Give the fake request a superuser so @login_required passes
        su = User.objects.filter(is_superuser=True).first()
        if su is None:
            su = User.objects.first()
        req.user = su
        # Temporarily disable graph builder flag for the legacy call
        import connect.lab_topology_views as _ltv
        orig = _ltv.TOPOLOGY_GRAPH_FABRIC
        _ltv.TOPOLOGY_GRAPH_FABRIC = False
        try:
            legacy_resp = lab_topology_views.lab_fabric_map_api(req, topo_id)
        finally:
            _ltv.TOPOLOGY_GRAPH_FABRIC = orig
        import json as _json
        legacy = _json.loads(legacy_resp.content)
    except Exception as exc:
        legacy = {'nodes': [], 'links': [], '_error': str(exc)}

    builder = TopologyGraphBuilder(topo_id=topo_id)
    graph = builder.build(live=live, lldp=lldp, use_cache=False)
    graph_fabric = builder.to_fabric_legacy(graph)

    legacy_links = len(legacy.get('links') or [])
    graph_links = len(graph_fabric.get('links') or [])
    legacy_nodes = len(legacy.get('nodes') or [])
    graph_nodes = len(graph_fabric.get('nodes') or [])

    def _link_pairs(data):
        pairs = set()
        for lk in data.get('links') or []:
            s, t = lk.get('source'), lk.get('target')
            if s and t:
                pairs.add(tuple(sorted((s, t))))
        return pairs

    legacy_pairs = _link_pairs(legacy)
    graph_pairs = _link_pairs(graph_fabric)
    only_legacy = legacy_pairs - graph_pairs
    only_graph = graph_pairs - legacy_pairs

    tolerance = max(5, int(legacy_links * 0.05)) if legacy_links else 5
    link_delta = abs(legacy_links - graph_links)
    ok = link_delta <= tolerance and abs(legacy_nodes - graph_nodes) <= 2

    return {
        'ok': ok,
        'topology_id': topo_id,
        'legacy': {'nodes': legacy_nodes, 'links': legacy_links},
        'graph': {'nodes': graph_nodes, 'links': graph_links},
        'delta': {
            'nodes': graph_nodes - legacy_nodes,
            'links': graph_links - legacy_links,
            'only_in_legacy_pairs': len(only_legacy),
            'only_in_graph_pairs': len(only_graph),
        },
        'tolerance': {'max_link_delta': tolerance},
        'sources': (graph.get('meta') or {}).get('sources', {}),
    }
