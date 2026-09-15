"""
Consolidated Lab Topology import/export (format version 3).

Preserves per-view layouts (designer, fabric, port_fabric) across re-import by
keying positions on stable ``node_key`` values, not ephemeral DB primary keys.
"""
from __future__ import annotations

import copy
import datetime as _dt
import re
from typing import Any, Dict, List, Optional, Tuple

from connect.ip_addressing import derive_dhcpv6_ocs_lab, mgmt_ip_bundle, mgmt_ip_bundle_from_entity
from connect.models import Device, KeysightChassis, LabTopology, LabTopologyLink, LabTopologyNode

FORMAT_ID = 'labvault.lab_topology'
EXPORT_VERSION = 3

_VIEW_LAYOUT_KEYS = frozenset({'designer', 'fabric', 'port_fabric'})
_NODE_ID_RE = re.compile(r'^node_(\d+)$')

_CABLE_COLORS = {
    'dac': '#ef4444',
    'optic': '#22c55e',
    'direct': '#3b82f6',
    'ocs': '#fb923c',
}

_NODE_TYPE_MAP = {
    'arista': 'switch',
    'sonic': 'switch',
    'keysight': 'chassis',
    'ocs': 'ocs',
    'fortigate': 'firewall',
    'paloalto': 'firewall',
}


def _stable_node_id(node: LabTopologyNode) -> str:
    key = (node.node_key or '').strip()
    if key:
        return key
    return str(node.pk)


def _export_view_layouts(topo: LabTopology, nodes: List[LabTopologyNode]) -> Dict[str, Any]:
    """Export view layouts with stable ``pos_by_node_key`` alongside legacy ``pos``."""
    extra_topo = dict(topo.extra or {})
    raw = copy.deepcopy(extra_topo.get('view_layouts') or {})
    key_by_pk = {n.pk: _stable_node_id(n) for n in nodes}
    out: Dict[str, Any] = {}
    for view_key, vdata in raw.items():
        if view_key not in _VIEW_LAYOUT_KEYS or not isinstance(vdata, dict):
            continue
        vd = copy.deepcopy(vdata)
        pos = vd.get('pos') if isinstance(vd.get('pos'), dict) else {}
        pos_by_key: Dict[str, Any] = dict(vd.get('pos_by_node_key') or {})
        for fabric_id, coords in pos.items():
            m = _NODE_ID_RE.match(str(fabric_id))
            if not m:
                continue
            pk = int(m.group(1))
            nkey = key_by_pk.get(pk)
            if nkey and nkey not in pos_by_key:
                pos_by_key[nkey] = coords
        vd['pos_by_node_key'] = pos_by_key
        out[view_key] = vd
    return out


def topology_to_api_dict(topo: LabTopology) -> Dict[str, Any]:
    """JSON for live designer/fabric APIs (DB primary keys on nodes and links)."""
    from connect.models import KeysightChassis as _KC

    nodes = []
    for n in topo.nodes.select_related('device').all():
        dev_ip = n.device.ip_address if n.device else None
        dev_vendor = n.device.vendor_type if n.device else None
        ports = list((n.extra or {}).get('ports') or [])
        dev_secondary = (n.device.vendor_type_secondary or '') if n.device else ''
        extra_out = dict(n.extra or {})
        if dev_secondary and 'vendor_type_secondary' not in extra_out:
            extra_out['vendor_type_secondary'] = dev_secondary
        entity = n.device
        if not entity and extra_out.get('chassis_id'):
            entity = _KC.objects.filter(pk=extra_out['chassis_id']).first()
        if entity:
            ip_fields = mgmt_ip_bundle_from_entity(entity)
        else:
            ip_fields = mgmt_ip_bundle(
                ipv4=dev_ip or extra_out.get('device_ip') or '',
                ipv6=extra_out.get('mgmt_ipv6') or '',
                preferred=extra_out.get('preferred_ip_version') or 'ipv4',
                hostname=extra_out.get('hostname') or n.label or '',
                ipv6_source=extra_out.get('mgmt_ipv6_source') or '',
            )
        nodes.append({
            'id': n.pk,
            'node_key': _stable_node_id(n),
            'label': n.label,
            'node_type': n.node_type,
            'device_ip': ip_fields.get('device_ip') or dev_ip,
            'mgmt_ipv4': ip_fields.get('mgmt_ipv4', ''),
            'mgmt_ipv6': ip_fields.get('mgmt_ipv6', ''),
            'mgmt_display': ip_fields.get('mgmt_display', ''),
            'preferred_ip_version': ip_fields.get('preferred_ip_version', 'ipv4'),
            'device_id': n.device_id,
            'vendor_type': dev_vendor,
            'vendor_type_secondary': dev_secondary,
            'x': n.x,
            'y': n.y,
            'ports': ports,
            'extra': extra_out,
        })
    links = []
    for lk in topo.links.select_related('node_a', 'node_b').all():
        links.append({
            'id': lk.pk,
            'node_a': lk.node_a_id,
            'port_a': lk.port_a,
            'node_b': lk.node_b_id,
            'port_b': lk.port_b,
            'cable_type': lk.cable_type,
            'color': lk.color or _CABLE_COLORS.get(lk.cable_type, '#888'),
            'label': lk.label,
            'extra': lk.extra or {},
        })
    extra_topo = dict(topo.extra or {})
    return {
        'version': 2,
        'id': topo.pk,
        'name': topo.name,
        'description': topo.description,
        'source': topo.source,
        'tags': topo.tags,
        'metrics_collection_enabled': topo.metrics_collection_enabled,
        'nodes': nodes,
        'links': links,
        'view_layouts': extra_topo.get('view_layouts') or {},
    }


def export_topology(topo: LabTopology) -> Dict[str, Any]:
    """Build consolidated export dict (version 3) for download/re-import."""
    from connect.models import KeysightChassis as _KC

    node_qs = list(topo.nodes.select_related('device').all())
    nodes_out: List[Dict[str, Any]] = []
    for n in node_qs:
        dev_ip = n.device.ip_address if n.device else None
        dev_vendor = n.device.vendor_type if n.device else None
        ports = list((n.extra or {}).get('ports') or [])
        dev_secondary = ''
        if n.device:
            dev_secondary = n.device.vendor_type_secondary or ''
        extra_out = dict(n.extra or {})
        if dev_secondary and 'vendor_type_secondary' not in extra_out:
            extra_out['vendor_type_secondary'] = dev_secondary
        entity = n.device
        if not entity and extra_out.get('chassis_id'):
            entity = _KC.objects.filter(pk=extra_out['chassis_id']).first()
        if entity:
            ip_fields = mgmt_ip_bundle_from_entity(entity)
        else:
            ip_fields = mgmt_ip_bundle(
                ipv4=dev_ip or extra_out.get('device_ip') or '',
                ipv6=extra_out.get('mgmt_ipv6') or '',
                preferred=extra_out.get('preferred_ip_version') or 'ipv4',
                hostname=extra_out.get('hostname') or n.label or '',
                ipv6_source=extra_out.get('mgmt_ipv6_source') or '',
            )
        stable_id = _stable_node_id(n)
        nodes_out.append({
            'id': stable_id,
            'node_key': stable_id,
            '_db_id': n.pk,
            'label': n.label,
            'node_type': n.node_type,
            'device_ip': ip_fields.get('device_ip') or dev_ip,
            'mgmt_ipv4': ip_fields.get('mgmt_ipv4', ''),
            'mgmt_ipv6': ip_fields.get('mgmt_ipv6', ''),
            'mgmt_display': ip_fields.get('mgmt_display', ''),
            'preferred_ip_version': ip_fields.get('preferred_ip_version', 'ipv4'),
            'device_id': n.device_id,
            'vendor_type': dev_vendor,
            'vendor_type_secondary': dev_secondary,
            'x': n.x,
            'y': n.y,
            'ports': ports,
            'extra': extra_out,
        })

    pk_to_stable = {n.pk: _stable_node_id(n) for n in node_qs}
    links_out: List[Dict[str, Any]] = []
    for lk in topo.links.select_related('node_a', 'node_b').all():
        links_out.append({
            'id': lk.pk,
            'node_a': pk_to_stable.get(lk.node_a_id, str(lk.node_a_id)),
            'port_a': lk.port_a,
            'node_b': pk_to_stable.get(lk.node_b_id, str(lk.node_b_id)),
            'port_b': lk.port_b,
            'cable_type': lk.cable_type,
            'color': lk.color or _CABLE_COLORS.get(lk.cable_type, '#888'),
            'label': lk.label,
            'extra': lk.extra or {},
        })

    extra_topo = dict(topo.extra or {})
    resource_bundle = dict(extra_topo.get('resource_bundle') or {})
    if extra_topo.get('addressing_profile') and 'addressing_profile' not in resource_bundle:
        resource_bundle['addressing_profile'] = extra_topo['addressing_profile']
    if extra_topo.get('site_json') and 'site_json' not in resource_bundle:
        resource_bundle['site_json'] = extra_topo['site_json']
    if extra_topo.get('layout_schema') and 'layout_schema' not in resource_bundle:
        resource_bundle['layout_schema'] = extra_topo['layout_schema']

    return {
        'format': FORMAT_ID,
        'version': EXPORT_VERSION,
        'exported_at': _dt.datetime.utcnow().isoformat() + 'Z',
        'topology': {
            'id': topo.pk,
            'name': topo.name,
            'description': topo.description,
            'source': topo.source,
            'tags': topo.tags,
            'extra': {k: v for k, v in extra_topo.items() if k != 'view_layouts'},
        },
        'nodes': nodes_out,
        'links': links_out,
        'view_layouts': _export_view_layouts(topo, node_qs),
        'resource_bundle': resource_bundle,
        # Legacy v2 fields for older tooling
        'id': topo.pk,
        'name': topo.name,
        'description': topo.description,
        'source': topo.source,
        'tags': topo.tags,
    }


def _site_ip_map(site_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    ip_map: Dict[str, Dict[str, Any]] = {}
    ocs = site_data.get('ocs_controller', {})
    if ocs.get('ip'):
        ip_map[ocs['ip']] = {**ocs, 'node_type': 'ocs', 'vendor_type': 'ocs'}
    for sw in site_data.get('arista_switches', []):
        ip = sw.get('ip') or sw.get('ip_address', '')
        if ip:
            ip_map[ip] = {**sw, 'node_type': 'switch', 'vendor_type': 'arista'}
    for sw in site_data.get('ares_switches', []):
        ip = sw.get('ip') or sw.get('ip_address', '')
        if ip:
            ip_map[ip] = {**sw, 'node_type': 'switch', 'vendor_type': 'sonic'}
    for ch in site_data.get('keysight_chassis', []):
        ip = ch.get('ip') or ch.get('ip_address', '')
        if ip:
            ip_map[ip] = {**ch, 'node_type': 'chassis', 'vendor_type': 'keysight'}
    return ip_map


def _enrich_extra_from_site(
    extra: Dict[str, Any],
    device_ip: str,
    site_info: Dict[str, Any],
    *,
    addressing_profile: str = '',
) -> None:
    """Apply OCS site JSON + v6 profile fields onto node.extra."""
    from connect.ocs_site_config_io import (
        _mgmt_ipv6_from_block,
        _ipv6_source_from_block,
        _preferred_ip_from_block,
    )

    if not site_info and addressing_profile != 'v6':
        return
    block = dict(site_info) if site_info else {}
    if device_ip and 'ip' not in block:
        block['ip'] = device_ip
    data = {'addressing': {'preferred_ip_version': 'ipv6', 'ipv6_mode': 'dhcpv6'}}
    if addressing_profile == 'v6' or (site_info and site_info.get('preferred_ip_version') in ('ipv6', 'dual')):
        v6 = _mgmt_ipv6_from_block(block, data) or derive_dhcpv6_ocs_lab(device_ip)
        if v6:
            extra.setdefault('mgmt_ipv6', v6)
            extra.setdefault('mgmt_ipv6_source', _ipv6_source_from_block(block, data) or 'dhcpv6')
        extra.setdefault('preferred_ip_version', _preferred_ip_from_block(block, data) or 'ipv6')
    elif site_info:
        v6 = _mgmt_ipv6_from_block(block, {'addressing': site_info.get('addressing') or {}})
        if v6:
            extra.setdefault('mgmt_ipv6', v6)
        src = _ipv6_source_from_block(block, {'addressing': site_info.get('addressing') or {}})
        if src:
            extra.setdefault('mgmt_ipv6_source', src)
        pref = _preferred_ip_from_block(block, {'addressing': site_info.get('addressing') or {}})
        extra.setdefault('preferred_ip_version', pref)

    mapping = site_info.get('port_to_ocs_triplets') or {}
    if not mapping:
        fixed = site_info.get('fixed_mapping') or {}
        mapping = fixed.get('port_to_ocs_triplets') or {}
    if mapping and not extra.get('ocs_triplet_map'):
        extra['ocs_triplet_map'] = mapping


def _remap_view_layouts(
    topo: LabTopology,
    exported_layouts: Dict[str, Any],
    id_map: Dict[str, LabTopologyNode],
    *,
    legacy_pk_map: Optional[Dict[int, int]] = None,
) -> Dict[str, Any]:
    """Rebuild view_layouts with ``pos`` keyed by new ``node_{pk}``."""
    if not exported_layouts:
        return {}
    key_to_node = {(n.node_key or '').strip(): n for n in id_map.values() if (n.node_key or '').strip()}
    key_to_node.update({k: v for k, v in id_map.items()})
    stored: Dict[str, Any] = {}
    now = _dt.datetime.utcnow().isoformat() + 'Z'

    for view_key, vdata in exported_layouts.items():
        if view_key not in _VIEW_LAYOUT_KEYS or not isinstance(vdata, dict):
            continue
        vd = copy.deepcopy(vdata)
        new_pos: Dict[str, Any] = {}
        pos_by_key = vd.get('pos_by_node_key') if isinstance(vd.get('pos_by_node_key'), dict) else {}
        for nkey, coords in pos_by_key.items():
            node = key_to_node.get(nkey)
            if node and isinstance(coords, dict):
                new_pos[f'node_{node.pk}'] = coords
        old_pos = vd.get('pos') if isinstance(vd.get('pos'), dict) else {}
        for fabric_id, coords in old_pos.items():
            m = _NODE_ID_RE.match(str(fabric_id))
            if not m or not isinstance(coords, dict):
                continue
            old_pk = int(m.group(1))
            node = None
            if legacy_pk_map and old_pk in legacy_pk_map:
                new_pk = legacy_pk_map[old_pk]
                node = topo.nodes.filter(pk=new_pk).first()
            if node:
                new_pos[f'node_{node.pk}'] = coords
        vd['pos'] = new_pos
        vd['pos_by_node_key'] = pos_by_key
        vd['schema_version'] = vd.get('schema_version', 2)
        vd['saved_at'] = now
        stored[view_key] = vd
    return stored


def _resolve_node_refs(ref: Any, id_map: Dict[str, LabTopologyNode]) -> Optional[LabTopologyNode]:
    key = str(ref or '').strip()
    if not key:
        return None
    if key in id_map:
        return id_map[key]
    if key.isdigit():
        return id_map.get(key)
    return None


def import_topology(
    topo: LabTopology,
    payload: Dict[str, Any],
    *,
    site_data: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Replace nodes/links on ``topo`` from export payload; preserve view layouts and metadata.

    Supports format v3 (consolidated) and legacy v2 exports.
    """
    is_v3 = payload.get('format') == FORMAT_ID or payload.get('version', 0) >= 3
    topo_meta = payload.get('topology') if is_v3 else {}
    if not isinstance(topo_meta, dict):
        topo_meta = {}

    if topo_meta.get('name'):
        topo.name = str(topo_meta['name'])
    if 'description' in topo_meta:
        topo.description = topo_meta.get('description') or ''
    if topo_meta.get('tags') is not None:
        topo.tags = topo_meta.get('tags') or ''
    if topo_meta.get('source'):
        topo.source = topo_meta['source']

    resource_bundle = dict(payload.get('resource_bundle') or {})
    extra_topo = dict(topo.extra or {})
    extra_topo.update(topo_meta.get('extra') or {})
    if resource_bundle.get('addressing_profile'):
        extra_topo['addressing_profile'] = resource_bundle['addressing_profile']
    if resource_bundle.get('site_json'):
        extra_topo['site_json'] = resource_bundle['site_json']
    if resource_bundle.get('layout_schema'):
        extra_topo['layout_schema'] = resource_bundle['layout_schema']
    addressing_profile = (extra_topo.get('addressing_profile') or '').strip().lower()

    if site_data is None and extra_topo.get('site_json'):
        from pathlib import Path

        site_path = Path(__file__).resolve().parent.parent / 'resources' / extra_topo['site_json']
        if site_path.is_file():
            import json

            try:
                site_data = json.loads(site_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                site_data = {}
    site_data = site_data or {}
    site_ip_map = _site_ip_map(site_data) if site_data else {}

    nodes_raw = payload.get('nodes') or []
    links_raw = payload.get('links') or []
    legacy_pk_map: Dict[int, int] = {}

    topo.nodes.all().delete()
    id_map: Dict[str, LabTopologyNode] = {}

    for nd in nodes_raw:
        stable_id = str(
            nd.get('node_key') or nd.get('id') or nd.get('label') or '',
        ).strip()
        if not stable_id:
            stable_id = f'node_{len(id_map)}'
        dev_ip = (nd.get('device_ip') or nd.get('mgmt_ipv4') or '').strip()
        device = Device.objects.filter(ip_address=dev_ip).first() if dev_ip else None
        chassis = KeysightChassis.objects.filter(ip_address=dev_ip).first() if dev_ip else None
        node_type = nd.get('node_type') or 'switch'
        if device and not nd.get('node_type'):
            node_type = _NODE_TYPE_MAP.get(device.vendor_type, 'switch')

        extra = dict(nd.get('extra') or {})
        site_info = site_ip_map.get(dev_ip, {})
        _enrich_extra_from_site(extra, dev_ip, site_info, addressing_profile=addressing_profile)
        for field in ('mgmt_ipv6', 'mgmt_ipv6_source', 'preferred_ip_version', 'hostname'):
            if nd.get(field) and field not in extra:
                extra[field] = nd[field]
        if dev_ip:
            extra['device_ip'] = extra.get('device_ip') or dev_ip
            extra['mgmt_ipv4'] = extra.get('mgmt_ipv4') or nd.get('mgmt_ipv4') or dev_ip
        # Always bind by management IP. Pickup JSON lists AresONE as both Device
        # and KeysightChassis; a Device hit used to skip the chassis lookup, so
        # Pulse never collected after import. Stale export PKs are overwritten.
        if chassis is None and extra.get('chassis_id'):
            chassis = KeysightChassis.objects.filter(pk=extra.get('chassis_id')).first()
        if chassis:
            extra['chassis_id'] = chassis.pk
            extra['chassis_type'] = chassis.chassis_type or extra.get('chassis_type') or ''
        elif node_type == 'chassis':
            extra.pop('chassis_id', None)
        ports = nd.get('ports')
        if ports:
            extra['ports'] = ports

        n_obj = LabTopologyNode.objects.create(
            topology=topo,
            device=device,
            node_key=stable_id[:64],
            node_type=node_type,
            label=nd.get('label') or stable_id or 'Node',
            x=float(nd.get('x') or 0),
            y=float(nd.get('y') or 0),
            extra=extra,
        )
        id_map[stable_id] = n_obj
        old_db = nd.get('_db_id')
        if old_db is not None:
            try:
                legacy_pk_map[int(old_db)] = n_obj.pk
            except (TypeError, ValueError):
                pass
        if str(nd.get('id', '')).isdigit() and int(nd['id']) not in legacy_pk_map:
            legacy_pk_map[int(nd['id'])] = n_obj.pk

    link_count = 0
    for lk in links_raw:
        na = _resolve_node_refs(lk.get('node_a'), id_map)
        nb = _resolve_node_refs(lk.get('node_b'), id_map)
        if not na or not nb:
            continue
        cable_type = lk.get('cable_type') or 'dac'
        LabTopologyLink.objects.create(
            topology=topo,
            node_a=na,
            port_a=lk.get('port_a') or '',
            node_b=nb,
            port_b=lk.get('port_b') or '',
            cable_type=cable_type,
            color=lk.get('color') or _CABLE_COLORS.get(cable_type, ''),
            label=lk.get('label') or '',
            extra=lk.get('extra') or {},
        )
        link_count += 1

    exported_layouts = payload.get('view_layouts') or {}
    if exported_layouts:
        remapped = _remap_view_layouts(topo, exported_layouts, id_map, legacy_pk_map=legacy_pk_map)
        extra_topo['view_layouts'] = remapped

    if 'metrics_collection_enabled' in topo_meta:
        topo.metrics_collection_enabled = bool(topo_meta['metrics_collection_enabled'])
    else:
        topo.metrics_collection_enabled = True

    topo.extra = extra_topo
    topo.save(update_fields=[
        'name', 'description', 'tags', 'source', 'extra',
        'metrics_collection_enabled', 'updated_at',
    ])

    return f'Imported {len(nodes_raw)} nodes, {link_count} links (views preserved: {bool(exported_layouts)}).'


def resource_paths_for_profile(profile: str) -> Tuple[str, str]:
    """Return (layout_schema_filename, site_json_filename) for a DC preset profile."""
    p = (profile or 'hbg').strip().lower()
    if p in ('v6', 'hbg-v6', 'ipv6', 'dc-v6'):
        return 'lab_topology_schema_v6.json', 'ocs_photonic_site.json'
    return 'lab_topology_schema.json', 'ocs_photonic_site.json'
