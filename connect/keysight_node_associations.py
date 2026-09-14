"""Mgmt/compute node association data for KCOS chassis (dashboard + BMC views)."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from django.core.cache import cache

from .cache_utils import cache_get, cache_set

from .keysight_aps_standalone import (
    is_standalone_hostname,
    is_standalone_node_inventory,
    standalone_aps_generation,
    standalone_chassis_index,
    standalone_cn_identity,
)
from .keysight_drivers import KCOS_TYPES
from .models import KeysightChassis

logger = logging.getLogger(__name__)

_NODE_ASSOC_CACHE_KEY = 'keysight:node_assoc:v1'
_NODE_ASSOC_TTL = 180  # seconds


def _node_entry_from_raw(node: dict) -> dict:
    from .keysight_aps_generations import node_aps_generation

    status = node.get('status', '') or ''
    role = node.get('role', '') or ''
    name = node.get('node_name', '') or ''
    chassis_hostname = node.get('chassis_hostname', '') or ''
    return {
        'name': name,
        'role': role,
        'status': status,
        'is_ready': status == 'Ready',
        'internal_ip': node.get('internal_ip', '') or '',
        'bmc_ip': node.get('bmc_ip', '') or '',
        'bmc_hostname': node.get('bmc_hostname', '') or '',
        'bmc_power': node.get('bmc_power', '') or '',
        'aps_gen': node_aps_generation(
            name, role, chassis_hostname=chassis_hostname,
            fru_board_product=node.get('fru_board_product', ''),
            fru_product_name=node.get('fru_product_name', ''),
            aps_gen_hint=node.get('aps_gen', ''),
        ) or standalone_aps_generation(chassis_hostname, name),
        'operating_mode': node.get('operating_mode', '') or '',
        'combined_cn': node.get('operating_mode') == 'standalone_merged',
        'chassis_hostname': chassis_hostname,
    }


def _split_mgmt_compute(nodes: list[dict]) -> tuple[dict | None, list[dict]]:
    if is_standalone_node_inventory(nodes):
        entry = _node_entry_from_raw(nodes[0])
        entry['combined_cn'] = True
        return entry, []

    mgmt = None
    compute: list[dict] = []
    for node in nodes:
        entry = _node_entry_from_raw(node)
        role = node.get('role', '')
        if role in ('Management', 'Standalone'):
            mgmt = entry
        else:
            compute.append(entry)
    return mgmt, compute


def build_association_for_chassis(ch, raw) -> dict:
    """Shape one chassis row for templates (matches BMC Associations)."""
    out = {
        'chassis_id': ch.id,
        'mgmt_node': None,
        'compute_nodes': [],
        'error': None,
        'mgmt_count': 0,
        'compute_count': 0,
        'up_count': 0,
        'down_count': 0,
    }
    if ch.status != 'online':
        out['error'] = f'Chassis {ch.status}'
        if ch.chassis_type == 'aps_standalone' or is_standalone_hostname(ch.hostname or ''):
            cn_id = standalone_cn_identity(ch.hostname or ch.ip_address or '')
            out['standalone'] = True
            out['cn_identity'] = cn_id
            out['mgmt_node'] = {
                'name': 'mgmt',
                'role': 'Standalone',
                'status': '',
                'is_ready': False,
                'cn_identity': cn_id,
                'aps_gen': standalone_aps_generation(ch.hostname or '', ''),
                'combined_cn': True,
                'operating_mode': 'standalone_merged',
            }
            out['mgmt_count'] = 1
        return out
    if isinstance(raw, dict) and 'error' in raw:
        out['error'] = raw.get('error') or 'Unavailable'
        return out
    if not isinstance(raw, list):
        out['error'] = 'No node data'
        return out
    mgmt, compute = _split_mgmt_compute(raw)
    out['mgmt_node'] = mgmt
    out['compute_nodes'] = compute
    out['standalone'] = bool(mgmt and mgmt.get('combined_cn'))
    if out['standalone'] and ch:
        cn_id = standalone_cn_identity(ch.hostname or ch.ip_address or '')
        out['cn_identity'] = cn_id
        if mgmt:
            mgmt['cn_identity'] = cn_id
            if not mgmt.get('aps_gen'):
                mgmt['aps_gen'] = standalone_aps_generation(ch.hostname or '', mgmt.get('name', ''))
    out['mgmt_count'] = 1 if mgmt else 0
    out['compute_count'] = len(compute)
    for n in ([mgmt] if mgmt else []) + compute:
        if n.get('is_ready'):
            out['up_count'] += 1
        else:
            out['down_count'] += 1
    return out


def fetch_node_inventory_for_chassis(ch: KeysightChassis, fetch_fn) -> list[dict] | dict:
    """Call injected fetch_fn (typically keysight_views._fetch_bmc_data_from_chassis)."""
    try:
        return fetch_fn(ch)
    except Exception as exc:
        return {'error': str(exc), 'chassis': ch.hostname or ch.ip_address}


def refresh_node_associations(
    chassis_list: list[KeysightChassis],
    fetch_fn,
    *,
    max_workers: int = 6,
) -> dict[int, dict]:
    """Fetch node inventory for online KCOS chassis; return map chassis_id -> assoc dict."""
    kcos = [ch for ch in chassis_list if ch.chassis_type in KCOS_TYPES]
    online = [ch for ch in kcos if ch.status == 'online']
    by_id: dict[int, dict] = {}

    for ch in kcos:
        if ch.status != 'online':
            by_id[ch.id] = build_association_for_chassis(ch, None)

    if online:
        with ThreadPoolExecutor(max_workers=min(len(online), max_workers)) as pool:
            futures = {pool.submit(fetch_node_inventory_for_chassis, ch, fetch_fn): ch for ch in online}
            for future in futures:
                ch = futures[future]
                raw = future.result()
                by_id[ch.id] = build_association_for_chassis(ch, raw)

    enrich_associations_with_standalone_links(by_id, chassis_list)

    payload = {'ts': time.time(), 'by_id': by_id}
    cache_set(_NODE_ASSOC_CACHE_KEY, payload, _NODE_ASSOC_TTL)
    return by_id


def get_cached_node_associations(chassis_ids: list[int] | None = None) -> dict[int, dict]:
    payload = cache_get(_NODE_ASSOC_CACHE_KEY)
    if not payload or not isinstance(payload, dict):
        return {}
    by_id = payload.get('by_id') or {}
    if chassis_ids is None:
        return dict(by_id)
    return {cid: by_id[cid] for cid in chassis_ids if cid in by_id}


def node_slot_counts_by_chassis_id(by_id: dict[int, dict]) -> dict[int, dict]:
    return {
        cid: {
            'mgmt': assoc.get('mgmt_count', 0),
            'compute': assoc.get('compute_count', 0),
        }
        for cid, assoc in by_id.items()
    }


def enrich_associations_with_standalone_links(
    by_id: dict[int, dict],
    chassis_list: list[KeysightChassis],
) -> None:
    """Cross-link CN slots on multi-node chassis with paired standalone appliance records."""
    from .models import KeysightBmcEndpoint

    index = standalone_chassis_index(chassis_list)
    bmc_by_hostname = {
        ep.hostname.lower(): ep
        for ep in KeysightBmcEndpoint.objects.exclude(hostname='')
    }
    if not index and not bmc_by_hostname:
        return
    for ch in chassis_list:
        assoc = by_id.get(ch.id)
        if not assoc:
            continue
        if assoc.get('standalone'):
            continue
        for cn in assoc.get('compute_nodes') or []:
            cn_name = (cn.get('name') or '').lower()
            paired = index.get(cn_name)
            if paired and paired.id != ch.id:
                cn['paired_standalone_chassis_id'] = paired.id
                cn['paired_standalone_hostname'] = paired.hostname or paired.ip_address
                cn['paired_standalone_type'] = paired.get_chassis_type_display()
                cn['standalone_capable'] = True
            bmc_hn = (cn.get('bmc_hostname') or '').lower()
            ep = bmc_by_hostname.get(bmc_hn) if bmc_hn else None
            if ep:
                if ep.aps_gen in ('10', '15'):
                    cn['aps_gen'] = ep.aps_gen
                if ep.fru_board_product:
                    cn['fru_board_product'] = ep.fru_board_product
                cn['hardware_error_reported'] = ep.hardware_error_reported
                cn['hardware_error_notes'] = ep.hardware_error_notes or ''
            if cn.get('standalone_capable'):
                continue
            if cn_name.startswith('cn-aps-o2-') or cn_name.startswith('cn-aps-o15-') or cn_name.startswith('cn-aps-o1-'):
                cn['standalone_capable'] = True
        mgmt = assoc.get('mgmt_node')
        if mgmt:
            bmc_hn = (mgmt.get('bmc_hostname') or '').lower()
            ep = bmc_by_hostname.get(bmc_hn) if bmc_hn else None
            if ep and ep.aps_gen in ('10', '15'):
                mgmt['aps_gen'] = ep.aps_gen
            if ep:
                mgmt['hardware_error_reported'] = ep.hardware_error_reported
                mgmt['hardware_error_notes'] = ep.hardware_error_notes or ''


def aggregate_slot_stats(by_id: dict[int, dict]) -> dict:
    mgmt = compute = up = down = 0
    for assoc in by_id.values():
        mgmt += assoc.get('mgmt_count', 0)
        compute += assoc.get('compute_count', 0)
        up += assoc.get('up_count', 0)
        down += assoc.get('down_count', 0)
    return {
        'total_mgmt': mgmt,
        'total_compute': compute,
        'total_nodes_up': up,
        'total_nodes_down': down,
    }
