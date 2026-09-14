"""Lab Topology Designer views - separate module to keep views.py clean."""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .models import (
    Device, KeysightChassis, LabTopology, LabTopologyLink, LabTopologyNode,
    TestSetupTemplate, TestSetupRun,
)
from . import ocs_helpers
from .topology_flags import TOPOLOGY_GRAPH_FABRIC
from .topology_audit import log_topology_action
from .keysight_views import _api_auth_required

logger = logging.getLogger(__name__)


def _schedule_switch_lldp_refresh(topo_id: int) -> None:
    """Background refresh_lldp for switches in one topology (scoped, non-blocking)."""
    try:
        from django.core.management import call_command
        from .topology_graph import switch_ips_for_topology

        switch_ips = switch_ips_for_topology(topo_id)
        if not switch_ips:
            return
        threading.Thread(
            target=lambda: call_command(
                'refresh_lldp', ip=switch_ips, topo_id=topo_id, verbosity=0,
            ),
            daemon=True,
            name=f'lldp-refresh-{topo_id}',
        ).start()
    except Exception as exc:
        logger.warning('topology switch LLDP refresh: %s', exc)


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


_VIEW_LAYOUT_KEYS = {'designer', 'fabric', 'port_fabric', 'usage'}
_VIEW_LAYOUT_SCHEMA_VERSION = 2


def _merge_view_layouts(topo: 'LabTopology', incoming: dict) -> None:
    """Merge per-view layout data into topo.extra['view_layouts'] atomically.

    Schema per view key (e.g. 'port_fabric'):
      {
        "schema_version": 2,
        "layout_mode": "dc" | "tier" | "free",
        "bundled": bool,
        "pos": { "<device_node_id>": {"x": float, "y": float} },
        "theme": "dark" | "grey" | "light",
        "b2b_labels": bool,
        "lldp_view": bool,
        "saved_at": "<iso>",
        "viewport": {"scale": float, "tx": float, "ty": float}
      }
    """
    import datetime as _dt
    if not isinstance(incoming, dict):
        return
    extra = dict(topo.extra or {})
    stored = dict(extra.get('view_layouts') or {})
    now = _dt.datetime.utcnow().isoformat() + 'Z'

    for view_key, vdata in incoming.items():
        if view_key not in _VIEW_LAYOUT_KEYS:
            continue
        if not isinstance(vdata, dict):
            continue
        prev = dict(stored.get(view_key) or {})
        prev.update({k: v for k, v in vdata.items() if v is not None})
        prev['schema_version'] = _VIEW_LAYOUT_SCHEMA_VERSION
        prev['saved_at'] = now
        stored[view_key] = prev

    extra['view_layouts'] = stored
    topo.extra = extra
    topo.save(update_fields=['extra', 'updated_at'])


def _parse_metrics_collection_flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _topo_to_dict(topo):
    from connect.lab_topology_io import topology_to_api_dict

    return topology_to_api_dict(topo)


def _import_payload(topo, payload, *, site_data=None):
    from connect.lab_topology_io import import_topology

    return import_topology(topo, payload, site_data=site_data)


def _default_ports_for_node(node_type: str, port_count: int | None = None) -> List[str]:
    """Scaffold port labels when adding a node without an explicit port list."""
    if port_count and port_count > 0:
        return [f'port_{i}' for i in range(1, port_count + 1)]
    if node_type == 'ocs':
        return ['shelf-1', 'shelf-2', 'shelf-3']
    if node_type == 'chassis':
        return [f'port_{i}' for i in range(1, 33)]
    if node_type == 'switch':
        return [f'port_{i}' for i in range(9, 65)]
    if node_type == 'server':
        return [f'port_{i}' for i in range(1, 5)]
    return [f'port_{i}' for i in range(1, 9)]


def _resolve_node_binding(
    node_type: str,
    device_id: int | None = None,
    chassis_id: int | None = None,
    device_ip: str = '',
    label: str = '',
) -> tuple:
    """Resolve Device FK, KeysightChassis metadata, and effective node_type."""
    device = None
    extra: Dict[str, Any] = {}
    ip = (device_ip or '').strip()

    if device_id:
        device = Device.objects.filter(pk=device_id).first()
        if device:
            ip = device.ip_address or ip
            if not node_type or node_type == 'generic':
                node_type = _NODE_TYPE_MAP.get(device.vendor_type, 'switch')

    chassis = None
    if chassis_id:
        chassis = KeysightChassis.objects.filter(pk=chassis_id).first()
        if chassis:
            ip = chassis.ip_address or ip
            extra['chassis_id'] = chassis.pk
            extra['chassis_type'] = chassis.chassis_type or ''
            if not node_type or node_type == 'generic':
                node_type = 'chassis'

    if not device and not chassis and ip:
        device = Device.objects.filter(ip_address=ip).first()
        if device and (not node_type or node_type == 'generic'):
            node_type = _NODE_TYPE_MAP.get(device.vendor_type, 'switch')
        if not device and (node_type == 'chassis' or not node_type):
            chassis = KeysightChassis.objects.filter(ip_address=ip).first()
            if chassis:
                extra['chassis_id'] = chassis.pk
                extra['chassis_type'] = chassis.chassis_type or ''
                node_type = 'chassis'
        if not device and not chassis:
            extra['device_ip'] = ip

    if not ip and node_type == 'chassis' and label:
        ip = _chassis_name_to_ip_helper(label)
        if ip:
            extra.setdefault('device_ip', ip)
            chassis = KeysightChassis.objects.filter(ip_address=ip).first()
            if chassis:
                extra['chassis_id'] = chassis.pk
                extra['chassis_type'] = chassis.chassis_type or ''

    return device, extra, node_type or 'generic', ip


def _node_to_dict(n: LabTopologyNode) -> Dict[str, Any]:
    dev_ip = n.device.ip_address if n.device else (n.extra or {}).get('device_ip', '')
    if not dev_ip and n.node_type == 'chassis':
        dev_ip = _chassis_name_to_ip_helper(n.label)
    dev_vendor = n.device.vendor_type if n.device else None
    dev_secondary = (n.device.vendor_type_secondary or '') if n.device else ''
    extra_out = dict(n.extra or {})
    if dev_secondary and 'vendor_type_secondary' not in extra_out:
        extra_out['vendor_type_secondary'] = dev_secondary
    return {
        'id': n.pk,
        'label': n.label,
        'node_type': n.node_type,
        'device_ip': dev_ip,
        'device_id': n.device_id,
        'vendor_type': dev_vendor,
        'vendor_type_secondary': dev_secondary,
        'x': n.x,
        'y': n.y,
        'ports': list(extra_out.get('ports') or []),
        'extra': extra_out,
    }


def _link_to_dict(lk: LabTopologyLink) -> Dict[str, Any]:
    return {
        'id': lk.pk,
        'node_a': lk.node_a_id,
        'port_a': lk.port_a,
        'node_b': lk.node_b_id,
        'port_b': lk.port_b,
        'cable_type': lk.cable_type,
        'color': lk.color or _CABLE_COLORS.get(lk.cable_type, '#888'),
        'label': lk.label,
        'extra': lk.extra or {},
    }


@login_required
def lab_topology_inventory(request, topo_id):
    """Devices and chassis available to add to a topology."""
    topo = get_object_or_404(LabTopology, pk=topo_id)
    in_topo_device_ids = set()
    in_topo_chassis_ips = set()
    for n in topo.nodes.select_related('device').all():
        if n.device_id:
            in_topo_device_ids.add(n.device_id)
        ip = (n.extra or {}).get('device_ip', '')
        if ip:
            in_topo_chassis_ips.add(ip)
        if n.node_type == 'chassis':
            resolved = _chassis_name_to_ip_helper(n.label)
            if resolved:
                in_topo_chassis_ips.add(resolved)

    devices = []
    for d in Device.objects.all().order_by('hostname', 'ip_address'):
        devices.append({
            'id': d.id,
            'kind': 'device',
            'label': d.hostname or d.ip_address,
            'ip': d.ip_address,
            'vendor_type': d.vendor_type,
            'in_topology': d.id in in_topo_device_ids,
        })

    chassis = []
    for ch in KeysightChassis.objects.all().order_by('hostname', 'ip_address'):
        chassis.append({
            'id': ch.id,
            'kind': 'chassis',
            'label': ch.hostname or ch.ip_address,
            'ip': ch.ip_address,
            'chassis_type': ch.chassis_type,
            'in_topology': ch.ip_address in in_topo_chassis_ips,
        })

    return JsonResponse({
        'topology_id': topo.pk,
        'devices': devices,
        'chassis': chassis,
    })


@login_required
@require_http_methods(['POST'])
def lab_topology_node_add(request, topo_id):
    """Add a single node to an existing topology (incremental, no wipe)."""
    topo = get_object_or_404(LabTopology, pk=topo_id)
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')

    label = (body.get('label') or '').strip() or 'Node'
    node_type = (body.get('node_type') or 'generic').strip()
    device, extra, node_type, _ip = _resolve_node_binding(
        node_type,
        device_id=body.get('device_id'),
        chassis_id=body.get('chassis_id'),
        device_ip=body.get('device_ip') or '',
        label=label,
    )

    ports = body.get('ports')
    if ports:
        extra['ports'] = list(ports)
    elif not (extra.get('ports')):
        port_count = body.get('port_count')
        try:
            port_count = int(port_count) if port_count is not None else None
        except (TypeError, ValueError):
            port_count = None
        extra['ports'] = _default_ports_for_node(node_type, port_count)

    node_key = (body.get('node_key') or label or '').strip()[:64]
    n_obj = LabTopologyNode.objects.create(
        topology=topo,
        device=device,
        node_key=node_key,
        node_type=node_type,
        label=label,
        x=float(body.get('x') or 80),
        y=float(body.get('y') or 80),
        extra=extra,
    )
    topo.save(update_fields=['updated_at'])
    log_topology_action(request.user, 'node_added', topology=topo, detail=f'Added {label}', request=request)
    return JsonResponse({'ok': True, 'node': _node_to_dict(n_obj)}, status=201)


@login_required
@require_http_methods(['PATCH', 'DELETE'])
def lab_topology_node_detail(request, topo_id, node_id):
    """Update or remove one topology node."""
    topo = get_object_or_404(LabTopology, pk=topo_id)
    node = get_object_or_404(LabTopologyNode, pk=node_id, topology=topo)

    if request.method == 'DELETE':
        label = node.label
        node.delete()
        topo.save(update_fields=['updated_at'])
        log_topology_action(request.user, 'node_deleted', topology=topo, detail=f'Deleted {label}', request=request)
        return JsonResponse({'ok': True})

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')

    update_fields = []
    if 'label' in body:
        node.label = body['label']
        update_fields.append('label')
    if 'x' in body:
        node.x = float(body['x'])
        update_fields.append('x')
    if 'y' in body:
        node.y = float(body['y'])
        update_fields.append('y')
    if 'extra' in body:
        node.extra = body['extra']
        update_fields.append('extra')
    if 'ports' in body:
        extra = dict(node.extra or {})
        extra['ports'] = body['ports']
        node.extra = extra
        update_fields.append('extra')
    if any(k in body for k in ('device_id', 'chassis_id', 'device_ip', 'node_type')):
        device, extra, node_type, _ip = _resolve_node_binding(
            body.get('node_type') or node.node_type,
            device_id=body.get('device_id'),
            chassis_id=body.get('chassis_id'),
            device_ip=body.get('device_ip') or (node.extra or {}).get('device_ip', ''),
            label=node.label,
        )
        node.device = device
        node.node_type = node_type
        merged = dict(node.extra or {})
        merged.update(extra)
        if 'ports' in body:
            merged['ports'] = body['ports']
        node.extra = merged
        update_fields.extend(['device', 'node_type', 'extra'])

    if update_fields:
        node.save(update_fields=list(set(update_fields)))
        topo.save(update_fields=['updated_at'])
    return JsonResponse({'ok': True, 'node': _node_to_dict(node)})


@login_required
@require_http_methods(['POST'])
def lab_topology_link_add(request, topo_id):
    """Add a single link without replacing the topology."""
    topo = get_object_or_404(LabTopology, pk=topo_id)
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')

    def _resolve_node(ref):
        if ref is None:
            return None
        try:
            return topo.nodes.get(pk=int(ref))
        except (LabTopologyNode.DoesNotExist, TypeError, ValueError):
            return None

    na = _resolve_node(body.get('node_a'))
    nb = _resolve_node(body.get('node_b'))
    if not na or not nb:
        return JsonResponse({'ok': False, 'error': 'node_a and node_b must be valid node IDs'}, status=400)
    if na.pk == nb.pk:
        return JsonResponse({'ok': False, 'error': 'Cannot link a node to itself'}, status=400)

    cable_type = body.get('cable_type') or 'dac'
    lk = LabTopologyLink.objects.create(
        topology=topo,
        node_a=na,
        port_a=body.get('port_a') or '',
        node_b=nb,
        port_b=body.get('port_b') or '',
        cable_type=cable_type,
        color=body.get('color') or _CABLE_COLORS.get(cable_type, ''),
        label=body.get('label') or '',
        extra=body.get('extra') or {},
    )
    topo.save(update_fields=['updated_at'])
    log_topology_action(request.user, 'link_added', topology=topo,
                        detail=f'{na.label}:{lk.port_a} → {nb.label}:{lk.port_b}', request=request)
    return JsonResponse({'ok': True, 'link': _link_to_dict(lk)}, status=201)


@login_required
@require_http_methods(['PATCH', 'DELETE'])
def lab_topology_link_delete(request, topo_id, link_id):
    topo = get_object_or_404(LabTopology, pk=topo_id)
    lk = get_object_or_404(LabTopologyLink, pk=link_id, topology=topo)
    if request.method == 'DELETE':
        lk.delete()
        topo.save(update_fields=['updated_at'])
        return JsonResponse({'ok': True})
    # PATCH — update cable_type, label, port_a, port_b
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid JSON')
    fields = []
    for attr in ('port_a', 'port_b', 'cable_type', 'label'):
        if attr in body:
            setattr(lk, attr, body[attr])
            fields.append(attr)
    if 'extra' in body:
        lk.extra = body['extra']
        fields.append('extra')
    if fields:
        lk.save(update_fields=fields)
        topo.save(update_fields=['updated_at'])
    return JsonResponse({'ok': True, 'link': _link_to_dict(lk)})


@login_required
@require_http_methods(['POST'])
def lab_topology_build_inventory(request):
    """Create a new topology and add selected devices/chassis from inventory."""
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')

    name = (body.get('name') or '').strip() or 'New Lab Topology'
    topo = LabTopology.objects.create(
        name=name,
        description=body.get('description') or '',
        source='manual',
        tags=body.get('tags') or '',
        created_by=request.user,
    )

    auto_serial = body.get('auto_serial', False) in (True, '1', 'true', 'yes')
    auto_lldp = body.get('auto_lldp', False) in (True, '1', 'true', 'yes')
    full_ports = body.get('full_ports', True) not in (False, '0', 0)
    refresh_cache = body.get('refresh_cache', False) in (True, '1', 'true', 'yes')

    added = 0
    x_base, y_base = 80, 80
    for idx, did in enumerate(body.get('device_ids') or []):
        device = Device.objects.filter(pk=did).first()
        if not device:
            continue
        node_type = _NODE_TYPE_MAP.get(device.vendor_type, 'switch')
        LabTopologyNode.objects.create(
            topology=topo,
            device=device,
            node_key=f'dev_{device.pk}'[:64],
            node_type=node_type,
            label=device.hostname or device.ip_address,
            x=x_base + (idx % 4) * 280,
            y=y_base + (idx // 4) * 200,
            extra={'ports': _default_ports_for_node(node_type)},
        )
        added += 1

    chassis_start = added
    from pathlib import Path as _Path
    from .topology_dac_finder import (
        apply_lldp_links_to_topology,
        apply_serial_links_to_topology,
        discover_serial_links,
        enrich_topology_ports_from_cache,
        ports_from_chassis_and_site,
    )

    site_data: Dict = {}
    site_path = _Path(__file__).resolve().parent.parent / 'resources' / 'ocs_photonic_site.json'
    if site_path.is_file():
        try:
            site_data = json.loads(site_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass
    site_by_ip = {
        (ch.get('ip') or ch.get('ip_address', '')): ch
        for ch in site_data.get('keysight_chassis') or []
    }

    for jdx, cid in enumerate(body.get('chassis_ids') or []):
        chassis = KeysightChassis.objects.filter(pk=cid).first()
        if not chassis:
            continue
        idx = chassis_start + jdx
        extra: Dict[str, Any] = {
            'chassis_id': chassis.pk,
            'chassis_type': chassis.chassis_type or '',
            'device_ip': chassis.ip_address,
        }
        if full_ports:
            names, details = ports_from_chassis_and_site(
                chassis, site_by_ip.get(chassis.ip_address), refresh=refresh_cache,
            )
            extra['ports'] = names
            extra['port_details'] = details
        else:
            extra['ports'] = _default_ports_for_node('chassis')
        LabTopologyNode.objects.create(
            topology=topo,
            device=None,
            node_key=f'ch_{chassis.pk}'[:64],
            node_type='chassis',
            label=chassis.hostname or chassis.ip_address,
            x=x_base + (idx % 4) * 280,
            y=y_base + (idx // 4) * 200,
            extra=extra,
        )
        added += 1

    serial_stats = {}
    lldp_created = 0
    if auto_serial or auto_lldp:
        if auto_serial:
            disc = discover_serial_links(topo, refresh_cache=refresh_cache)
            serial_stats = apply_serial_links_to_topology(topo, disc['pairs'])
            serial_stats['pairs_found'] = len(disc['pairs'])
        if auto_lldp:
            lldp_created = apply_lldp_links_to_topology(topo)
    elif full_ports and body.get('chassis_ids'):
        enrich_topology_ports_from_cache(topo, site_data, refresh=refresh_cache)

    return JsonResponse({
        'ok': True,
        'topology_id': topo.pk,
        'name': topo.name,
        'nodes_added': added,
        'serial_links': serial_stats,
        'lldp_links': lldp_created,
        'redirect': f'/lab-topology/{topo.pk}/',
    }, status=201)


@login_required
def lab_topology_list(request):
    topos = list(LabTopology.objects.all().order_by('-updated_at'))
    child_ids = set()
    for t in topos:
        subs = (t.extra or {}).get('sub_topologies') or {}
        if not isinstance(subs, dict):
            continue
        for entry in subs.values():
            if isinstance(entry, dict) and entry.get('id'):
                child_ids.add(int(entry['id']))
    devices = Device.objects.all().order_by('hostname', 'ip_address')
    chassis_list = KeysightChassis.objects.all().order_by('hostname', 'ip_address')
    return render(request, 'connect/lab_topology_list.html', {
        'topos': topos,
        'child_topo_ids': child_ids,
        'devices': devices,
        'chassis_list': chassis_list,
    })


@login_required
def lab_topology_new(request):
    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip() or 'Untitled Topology'
        topo = LabTopology.objects.create(
            name=name,
            description=request.POST.get('description') or '',
            source='manual',
            tags=request.POST.get('tags') or '',
            created_by=request.user,
        )
        return redirect('lab_topology_detail', topo_id=topo.pk)
    topos = LabTopology.objects.all().order_by('-updated_at')
    return render(request, 'connect/lab_topology_list.html', {'topos': topos, 'show_new': True})


@login_required
def lab_topology_detail(request, topo_id):
    from connect.lab_topology_split import get_sub_topology_nav

    topo = get_object_or_404(LabTopology, pk=topo_id)
    devices = Device.objects.all().order_by('hostname', 'ip_address')
    chassis_list = KeysightChassis.objects.all().order_by('hostname', 'ip_address')
    sub_nav = get_sub_topology_nav(topo)
    return render(request, 'connect/lab_topology_detail.html', {
        'topo': topo,
        'devices': devices,
        'chassis_list': chassis_list,
        'sub_nav': sub_nav,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def lab_topology_data(request, topo_id):
    topo = get_object_or_404(LabTopology, pk=topo_id)
    if request.method == 'GET':
        return JsonResponse(_topo_to_dict(topo))
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')
    for nd in body.get('nodes') or []:
        nid = nd.get('id')
        if not nid:
            continue
        try:
            node = topo.nodes.get(pk=nid)
        except LabTopologyNode.DoesNotExist:
            continue
        update_fields = []
        if 'x' in nd:
            node.x = float(nd['x']); update_fields.append('x')
        if 'y' in nd:
            node.y = float(nd['y']); update_fields.append('y')
        if 'label' in nd:
            node.label = nd['label']; update_fields.append('label')
        if 'extra' in nd:
            node.extra = nd['extra']; update_fields.append('extra')
        if update_fields:
            node.save(update_fields=update_fields)
    # Persist link layout (positions stored on nodes; links are DB rows)
    for lk in body.get('links') or []:
        lid = lk.get('id')
        if not lid or str(lid).startswith('live_'):
            continue
        try:
            link = topo.links.get(pk=int(lid))
        except (LabTopologyLink.DoesNotExist, TypeError, ValueError):
            continue
        lfields = []
        for attr in ('port_a', 'port_b', 'cable_type', 'label', 'color'):
            if attr in lk:
                setattr(link, attr, lk[attr])
                lfields.append(attr)
        if 'extra' in lk:
            link.extra = lk['extra']
            lfields.append('extra')
        if lfields:
            link.save(update_fields=lfields)
    if 'name' in body:
        new_name = (body['name'] or '').strip()
        if new_name:
            topo.name = new_name
            topo.save(update_fields=['name', 'updated_at'])
    if 'metrics_collection_enabled' in body:
        topo.metrics_collection_enabled = _parse_metrics_collection_flag(
            body['metrics_collection_enabled'],
        )
        topo.save(update_fields=['metrics_collection_enabled', 'updated_at'])
    if 'view_layouts' in body:
        _merge_view_layouts(topo, body['view_layouts'])
    return JsonResponse({
        'ok': True,
        'name': topo.name,
        'metrics_collection_enabled': topo.metrics_collection_enabled,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def lab_topology_metrics_collection(request, topo_id):
    """GET/POST metrics collection pause for a topology (np_timeseries disk saver)."""
    topo = get_object_or_404(LabTopology, pk=topo_id)
    if request.method == 'GET':
        return JsonResponse({
            'ok': True,
            'topology_id': topo.pk,
            'metrics_collection_enabled': topo.metrics_collection_enabled,
        })
    try:
        body = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    if 'enabled' in body:
        enabled = _parse_metrics_collection_flag(body['enabled'])
    elif 'metrics_collection_enabled' in body:
        enabled = _parse_metrics_collection_flag(body['metrics_collection_enabled'])
    else:
        return JsonResponse({'ok': False, 'error': 'enabled is required'}, status=400)
    topo.metrics_collection_enabled = enabled
    topo.save(update_fields=['metrics_collection_enabled', 'updated_at'])
    return JsonResponse({
        'ok': True,
        'topology_id': topo.pk,
        'metrics_collection_enabled': topo.metrics_collection_enabled,
    })


@login_required
@require_http_methods(['POST'])
def lab_topology_split_subtopologies(request, topo_id):
    """Split HBG parent into DAC-staging (no OCS) and OCS-patched sub-topologies."""
    from connect.lab_topology_split import split_hbg_ocs_sub_topologies, get_sub_topology_nav

    parent = get_object_or_404(LabTopology, pk=topo_id)
    try:
        result = split_hbg_ocs_sub_topologies(parent, replace_existing=True)
    except Exception as exc:
        logger.exception('lab_topology_split_subtopologies failed parent=%s', topo_id)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)

    nav = get_sub_topology_nav(parent)
    log_topology_action(
        request.user, 'topology_split', topology=parent,
        detail=f'created sub-topologies: '
               f'without={result["without_ocs_patch"].pk} '
               f'with={result["with_ocs_patch"].pk}',
        request=request,
    )
    return JsonResponse({
        'ok': True,
        'parent_id': parent.pk,
        'sub_topologies': nav.get('sub_topologies') or [],
        'without_ocs_patch': {
            'id': result['without_ocs_patch'].pk,
            'name': result['without_ocs_patch'].name,
            'url': f'/lab-topology/{result["without_ocs_patch"].pk}/',
        },
        'with_ocs_patch': {
            'id': result['with_ocs_patch'].pk,
            'name': result['with_ocs_patch'].name,
            'url': f'/lab-topology/{result["with_ocs_patch"].pk}/',
        },
    })


@login_required
def lab_topology_clone(request, topo_id):
    """Duplicate a topology (nodes, links, view layouts, metadata)."""
    from connect.lab_topology_io import export_topology, import_topology

    src = get_object_or_404(LabTopology, pk=topo_id)
    try:
        body = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        body = {}

    base_name = (body.get('name') or '').strip() or f'{src.name} (copy)'
    name = base_name
    suffix = 2
    while LabTopology.objects.filter(name=name).exists():
        name = f'{base_name} ({suffix})'
        suffix += 1

    payload = export_topology(src)
    topo_meta = payload.get('topology') or {}
    if not isinstance(topo_meta, dict):
        topo_meta = {}
    topo_meta['name'] = name
    payload['topology'] = topo_meta

    topo = LabTopology.objects.create(
        name=name,
        description=src.description,
        source='clone',
        tags=src.tags,
        extra=copy.deepcopy(src.extra or {}),
        created_by=request.user,
    )
    try:
        msg = import_topology(topo, payload)
    except Exception as exc:
        logger.exception('lab_topology_clone failed src=%s', topo_id)
        topo.delete()
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)

    log_topology_action(
        request.user, 'topology_clone', topology=topo,
        detail=f'cloned from {src.pk} ({src.name}): {msg}',
        request=request,
    )
    return JsonResponse({
        'ok': True,
        'topo_id': topo.pk,
        'name': topo.name,
        'msg': msg,
        'redirect': f'/lab-topology/{topo.pk}/',
    })


@login_required
def lab_topology_export(request, topo_id):
    from connect.lab_topology_io import export_topology

    topo = get_object_or_404(LabTopology, pk=topo_id)
    payload = json.dumps(export_topology(topo), indent=2)
    resp = HttpResponse(payload, content_type='application/json')
    safe = re.sub(r'[^\w.\-]+', '_', topo.name)[:60]
    fname = f'{safe}.labtopo.v3.json'
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


@login_required
@require_http_methods(['POST'])
def lab_topology_import(request, topo_id):
    topo = get_object_or_404(LabTopology, pk=topo_id)
    uploaded = request.FILES.get('file')
    if uploaded:
        try:
            payload = json.load(uploaded)
        except (json.JSONDecodeError, ValueError) as exc:
            return HttpResponseBadRequest('Invalid JSON: {}'.format(exc))
    else:
        try:
            payload = json.loads(request.body)
        except (json.JSONDecodeError, ValueError) as exc:
            return HttpResponseBadRequest('Invalid JSON: {}'.format(exc))
    msg = _import_payload(topo, payload)
    if topo.source != 'import':
        topo.source = 'import'
        topo.save(update_fields=['source', 'updated_at'])
    return JsonResponse({'ok': True, 'msg': msg})


@login_required
@require_http_methods(['POST'])
def lab_topology_push_port_config(request, topo_id):
    topo = get_object_or_404(LabTopology, pk=topo_id)
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')
    node_id = body.get('node_id')
    commands = body.get('commands') or []
    if not node_id or not commands:
        return HttpResponseBadRequest('node_id and commands required')
    try:
        node = topo.nodes.select_related('device').get(pk=node_id)
    except LabTopologyNode.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Node not found'}, status=404)
    device = node.device
    if not device:
        return JsonResponse({'ok': False, 'error': 'Node has no bound device'})
    try:
        from .drivers import get_driver
        driver = get_driver(device)
        result = driver.send_config(commands)
        return JsonResponse({'ok': result.success, 'output': result.data, 'error': result.error})
    except Exception as exc:
        logger.exception('push_port_config topo=%s node=%s', topo_id, node_id)
        return JsonResponse({'ok': False, 'error': str(exc)})


@login_required
@require_http_methods(['GET', 'POST'])
def lab_topology_from_lldp(request):
    if request.method == 'GET':
        return redirect('lab_topology_list')
    import datetime
    try:
        from .topology import get_cached_topology
        topo_data = get_cached_topology() or {}
    except Exception:
        topo_data = {}
    raw_nodes = topo_data.get('nodes') or []
    raw_links = topo_data.get('links') or []
    devices_by_ip = {d.ip_address: d for d in Device.objects.all() if d.ip_address}
    topo = LabTopology.objects.create(
        name='From LLDP - ' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        source='lldp',
        created_by=request.user,
    )
    cols = 4
    node_map = {}
    for idx, nd in enumerate(raw_nodes):
        node_ip = nd.get('ip') or nd.get('id') or ''
        dev = devices_by_ip.get(node_ip)
        vendor = (dev.vendor_type if dev else '') or nd.get('vendor_type') or 'generic'
        node_type = _NODE_TYPE_MAP.get(vendor, 'switch')
        col = idx % cols
        row = idx // cols
        n_obj = LabTopologyNode.objects.create(
            topology=topo,
            device=dev,
            node_type=node_type,
            label=nd.get('label') or nd.get('hostname') or node_ip or 'Node-{}'.format(idx),
            x=120 + col * 280,
            y=80 + row * 200,
            extra={},
        )
        for key in (node_ip, nd.get('id'), nd.get('hostname'), nd.get('label')):
            if key and key not in node_map:
                node_map[key] = n_obj
    for lk in raw_links:
        a_key = lk.get('source') or lk.get('node_a') or ''
        b_key = lk.get('target') or lk.get('node_b') or ''
        na = node_map.get(a_key)
        nb = node_map.get(b_key)
        if not na or not nb or na == nb:
            continue
        port_a = lk.get('source_port') or lk.get('port_a') or ''
        port_b = lk.get('target_port') or lk.get('port_b') or ''
        cable = 'optic' if port_a.startswith('et-') else 'dac'
        LabTopologyLink.objects.get_or_create(
            topology=topo, node_a=na, port_a=port_a, node_b=nb, port_b=port_b,
            defaults={'cable_type': cable, 'color': _CABLE_COLORS.get(cable, '')},
        )
    return redirect('lab_topology_detail', topo_id=topo.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Site-topology import (upload two JSON files in one request)
# ─────────────────────────────────────────────────────────────────────────────

def _build_site_ip_map(site_data: Dict) -> Dict[str, Dict]:
    """Map IP → device metadata from OCS site JSON."""
    ip_map: Dict[str, Dict] = {}
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


def _import_topology_layout(topo: LabTopology, layout_data: Dict, site_data: Dict) -> str:
    """Create LabTopologyNode + LabTopologyLink records from a layout JSON dict."""
    site_ip_map = _build_site_ip_map(site_data)
    layout = layout_data.get('layout') or {}
    nodes_raw = layout.get('nodes') or []
    links_raw = layout.get('links') or []

    id_map: Dict[str, LabTopologyNode] = {}

    for nd in nodes_raw:
        node_id = str(nd.get('id') or nd.get('label') or 'node')
        label = nd.get('label') or node_id
        node_type = nd.get('node_type') or 'generic'
        device_ip = nd.get('device_ip') or ''

        site_info = site_ip_map.get(device_ip, {})
        if not node_type or node_type == 'generic':
            node_type = site_info.get('node_type', node_type)

        device = Device.objects.filter(ip_address=device_ip).first() if device_ip else None
        chassis = None
        if not device and node_type == 'chassis' and device_ip:
            chassis = KeysightChassis.objects.filter(ip_address=device_ip).first()

        extra = dict(nd.get('extra') or {})
        ports = list(nd.get('ports') or [])
        site_triplets = (
            (site_info.get('fixed_mapping') or {}).get('port_to_ocs_triplets')
            or site_info.get('port_to_ocs_triplets')
        )
        if site_triplets and len(ports) > 8:
            ports = []
        if ports:
            extra['ports'] = ports
        if chassis:
            extra['chassis_type'] = chassis.chassis_type
            extra['chassis_id'] = chassis.pk
        from connect.lab_topology_io import _enrich_extra_from_site

        profile = (topo.extra or {}).get('addressing_profile', '')
        _enrich_extra_from_site(extra, device_ip, site_info, addressing_profile=profile)

        n_obj = LabTopologyNode.objects.create(
            topology=topo,
            device=device,
            node_key=node_id[:64],
            node_type=node_type,
            label=label,
            x=float(nd.get('x') or 0),
            y=float(nd.get('y') or 0),
            extra=extra,
        )
        id_map[node_id] = n_obj

    link_count = 0
    for lk in links_raw:
        a_id = str(lk.get('from') or lk.get('node_a') or '')
        b_id = str(lk.get('to') or lk.get('node_b') or '')
        na = id_map.get(a_id)
        nb = id_map.get(b_id)
        if not na or not nb:
            continue
        cable_type = lk.get('cable_type') or 'dac'
        color = lk.get('color') or _CABLE_COLORS.get(cable_type, '#888')
        label_text = lk.get('label') or ''
        port_range_a = lk.get('port_range_a') or ''
        port_range_b = lk.get('port_range_b') or ''
        count = lk.get('count') or 0
        if not label_text and count:
            cable_name = 'optic' if cable_type == 'optic' else 'DAC'
            label_text = f'{count}× {cable_name}'

        link_extra: Dict = {}
        if port_range_a:
            link_extra['port_range_a'] = port_range_a
        if port_range_b:
            link_extra['port_range_b'] = port_range_b
        if count:
            link_extra['count'] = count

        LabTopologyLink.objects.create(
            topology=topo,
            node_a=na,
            port_a=port_range_a,
            node_b=nb,
            port_b=port_range_b,
            cable_type=cable_type,
            color=color,
            label=label_text,
            extra=link_extra,
        )
        link_count += 1

    return f'Imported {len(nodes_raw)} nodes and {link_count} links.'


@login_required
@require_http_methods(['POST'])
def lab_topology_import_site(request):
    """
    Accept a topology layout JSON (and optional OCS site JSON) via multipart upload
    and create a new LabTopology in the designer.

    POST fields:
      topo_file    — required, topology layout JSON file (or raw body JSON)
      site_file    — optional, OCS site JSON file
      name         — optional, override topology name
      replace      — optional, '1' to delete existing topology with same name
    """
    topo_file = request.FILES.get('topo_file')
    site_file = request.FILES.get('site_file')

    if topo_file:
        try:
            layout_data = json.load(topo_file)
        except (json.JSONDecodeError, ValueError) as exc:
            return JsonResponse({'ok': False, 'error': f'Invalid topology JSON: {exc}'}, status=400)
    else:
        try:
            layout_data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError) as exc:
            return JsonResponse({'ok': False, 'error': f'Invalid JSON body: {exc}'}, status=400)

    site_data: Dict = {}
    if site_file:
        try:
            site_data = json.load(site_file)
        except (json.JSONDecodeError, ValueError) as exc:
            return JsonResponse({'ok': False, 'error': f'Invalid site JSON: {exc}'}, status=400)

    name = (request.POST.get('name') or '').strip() or layout_data.get('label') or 'Imported Topology'
    do_replace = request.POST.get('replace') in ('1', 'true', 'yes')

    if do_replace:
        LabTopology.objects.filter(name=name).delete()

    topo = LabTopology.objects.create(
        name=name,
        description=layout_data.get('description') or '',
        source='import',
        created_by=request.user,
    )
    try:
        from connect.lab_topology_io import FORMAT_ID

        is_v3 = (
            layout_data.get('format') == FORMAT_ID
            or layout_data.get('version', 0) >= 3
            or (
                isinstance(layout_data.get('nodes'), list)
                and not (layout_data.get('layout') or {}).get('nodes')
            )
        )
        if is_v3:
            msg = _import_payload(topo, layout_data, site_data=site_data)
        else:
            msg = _import_topology_layout(topo, layout_data, site_data)
    except Exception as exc:
        logger.exception('lab_topology_import_site topo=%s', topo.pk)
        topo.delete()
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)

    return JsonResponse({
        'ok': True,
        'msg': msg,
        'topo_id': topo.pk,
        'redirect': f'/lab-topology/{topo.pk}/',
    })


# ─────────────────────────────────────────────────────────────────────────────
# _topo_to_dict patch: include extra on links
# ─────────────────────────────────────────────────────────────────────────────

def _topo_to_dict_v2(topo: LabTopology) -> Dict:
    """Enhanced version of _topo_to_dict that also serialises link.extra."""
    base = _topo_to_dict(topo)
    link_extra_map = {
        lk.pk: lk.extra or {}
        for lk in topo.links.all()
    }
    for lk_dict in base['links']:
        lk_dict['extra'] = link_extra_map.get(lk_dict['id'], {})
    return base


@login_required
@require_http_methods(['GET', 'POST'])
def lab_topology_data_v2(request, topo_id):
    """Drop-in replacement for lab_topology_data that includes link.extra."""
    topo = get_object_or_404(LabTopology, pk=topo_id)
    if request.method == 'GET':
        return JsonResponse(_topo_to_dict_v2(topo))
    # POST: delegate to original handler
    return lab_topology_data(request, topo_id)


# ─────────────────────────────────────────────────────────────────────────────
# Test Setup Builder views (Phase 2 + 3)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def test_setup_list(request, topo_id):
    """List all test setups for a topology + create form."""
    topo = get_object_or_404(LabTopology, pk=topo_id)
    templates = TestSetupTemplate.objects.filter(topology=topo).order_by('-updated_at')
    chassis_nodes = topo.nodes.filter(node_type='chassis').values('pk', 'label', 'extra')
    return render(request, 'connect/test_setup_builder.html', {
        'topo': topo,
        'templates': templates,
        'chassis_nodes': list(chassis_nodes),
        'speed_options': ['100G', '200G', '400G', '800G', '1.6T'],
    })


@login_required
@require_http_methods(['POST'])
def test_setup_calculate(request, topo_id):
    """
    Calculate resource requirements for a test setup.

    POST body (JSON):
      chassis_node_ids: [int, ...]
      port_count: int
      port_speed: "100G"|"400G"|"800G"
      test_type: "full_mesh"|"p2p"|"one_to_many"
      template_name: str (optional, save as new template)
    """
    topo = get_object_or_404(LabTopology, pk=topo_id)
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

    from .test_setup_engine import ResourceCalculator
    calc = ResourceCalculator()
    try:
        plan = calc.compute(topo, body)
    except Exception as exc:
        logger.exception('test_setup_calculate topo=%s', topo_id)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)

    template_name = (body.get('template_name') or '').strip()
    if template_name:
        tmpl, _ = TestSetupTemplate.objects.update_or_create(
            topology=topo, name=template_name,
            defaults={
                'requirements': body,
                'computed_plan': plan,
                'status': 'planned',
                'created_by': request.user,
            },
        )
        return JsonResponse({'ok': True, 'plan': plan, 'template_id': tmpl.pk})

    return JsonResponse({'ok': True, 'plan': plan})


@login_required
def test_setup_detail(request, template_id):
    """View/edit a saved test setup template."""
    tmpl = get_object_or_404(TestSetupTemplate, pk=template_id)
    return render(request, 'connect/test_setup_builder.html', {
        'topo': tmpl.topology,
        'template': tmpl,
        'chassis_nodes': list(
            tmpl.topology.nodes.filter(node_type='chassis').values('pk', 'label', 'extra')
        ) if tmpl.topology else [],
    })


@login_required
@require_http_methods(['POST'])
def test_setup_apply(request, template_id):
    """
    Trigger automated provisioning: OCS patches + switch configs.
    Runs in a background thread and returns a run_id for status polling.
    """
    tmpl = get_object_or_404(TestSetupTemplate, pk=template_id)
    if not tmpl.computed_plan:
        return JsonResponse({'ok': False, 'error': 'No computed plan — run Calculate first'}, status=400)

    from django.utils import timezone
    run = TestSetupRun.objects.create(
        template=tmpl,
        status='queued',
        started_at=timezone.now(),
    )
    tmpl.status = 'running'
    tmpl.save(update_fields=['status', 'updated_at'])

    def _run():
        from .test_setup_engine import SetupExecutor
        executor = SetupExecutor()
        executor.execute(run)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return JsonResponse({'ok': True, 'run_id': run.pk})


@login_required
def test_setup_run_status(request, run_id):
    """Poll status of a TestSetupRun."""
    run = get_object_or_404(TestSetupRun, pk=run_id)
    return JsonResponse({
        'ok': True,
        'run_id': run.pk,
        'status': run.status,
        'steps': run.steps or [],
        'log': run.log[-4000:] if run.log else '',
        'result_summary': run.result_summary or {},
        'started_at': run.started_at.isoformat() if run.started_at else None,
        'finished_at': run.finished_at.isoformat() if run.finished_at else None,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Scenario Analysis — "current vs needed" OCS patch plan
# ─────────────────────────────────────────────────────────────────────────────

def _load_full_port_map() -> Dict:
    """
    Load the complete port → OCS-triplet map from the OCS site JSON
    (all arista_switches + keysight_chassis entries).

    Returns: {ip: {name, vendor_type, ocs_ip, port_to_ocs_triplets: {port: [t1,t2,...]}}}
    """
    import json, os, re
    sites_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'resources')
    port_map: Dict = {}
    for fname in os.listdir(sites_dir):
        if not fname.startswith('ocs_photonic_site') or not fname.endswith('.json'):
            continue
        fpath = os.path.join(sites_dir, fname)
        try:
            with open(fpath) as fh:
                d = json.load(fh)
        except Exception:
            continue
        for sw in d.get('arista_switches', []):
            ip = sw.get('ip') or sw.get('ip_address', '')
            if not ip:
                continue
            mapping = sw.get('fixed_mapping', {})
            port_map[ip] = {
                'name': sw.get('name', ip),
                'vendor_type': 'arista',
                'ocs_ip': mapping.get('to_ocs', ''),
                'port_to_ocs_triplets': mapping.get('port_to_ocs_triplets', {}),
            }
        for ch in d.get('keysight_chassis', []):
            ip = ch.get('ip') or ch.get('ip_address', '')
            if not ip:
                continue
            notes = ch.get('notes', '')
            m = re.search(r'\[ocs_site_mapping\]\s*(\{.*?\})\s*$', notes, re.DOTALL)
            if m:
                try:
                    mapping = json.loads(m.group(1))
                    port_map[ip] = {
                        'name': ch.get('name', ip),
                        'vendor_type': 'keysight',
                        'ocs_ip': mapping.get('to_ocs', ''),
                        'port_to_ocs_triplets': mapping.get('port_to_ocs_triplets', {}),
                    }
                except Exception:
                    pass
    return port_map


def _get_current_ocs_xconnects(ocs_device) -> List[Dict]:
    """Fetch cached or live OCS cross-connects as [{name, port_a, port_b, state}]."""
    try:
        from django.core.cache import cache
        cached = cache.get(f'ocs_xconnects_{ocs_device.pk}')
        if cached is not None:
            return cached
        from .drivers import get_driver
        driver = get_driver(ocs_device)
        # OcsDriver exposes fetch_crossconnect_list → returns raw rows
        # then get_ocs_crossconnects for parsed output
        raw_rows = driver.fetch_crossconnect_list() if hasattr(driver, 'fetch_crossconnect_list') else []
        result = driver.get_ocs_crossconnects(raw_rows) if hasattr(driver, 'get_ocs_crossconnects') else None
        data = (result.data if result and hasattr(result, 'data') else raw_rows) or []
        xconns = []
        for xc in (data if isinstance(data, list) else []):
            # Normalise across different OCS driver return formats
            port_a = str(xc.get('port_a') or xc.get('portA') or xc.get('half1_port') or
                         (xc.get('h1') or {}).get('conn', '') or '')
            port_b = str(xc.get('port_b') or xc.get('portB') or xc.get('half2_port') or
                         (xc.get('h2') or {}).get('conn', '') or '')
            if not port_a and not port_b:
                continue
            xconns.append({
                'name': xc.get('name', '') or xc.get('xc_name', ''),
                'port_a': port_a.split('/')[0] if '/' in port_a else port_a,
                'port_b': port_b.split('/')[0] if '/' in port_b else port_b,
                'state': xc.get('state') or xc.get('status') or 'unknown',
            })
        cache.set(f'ocs_xconnects_{ocs_device.pk}', xconns, 60)
        return xconns
    except Exception as exc:
        logger.warning('Cannot get OCS cross-connects: %s', exc)
        return []


def _compute_scenario(topo, chassis_node_ids: List[int], switch_node_ids: List[int]) -> Dict:
    """
    Full scenario analysis:
    - For selected chassis: load their OCS triplet maps
    - Build port-connection table (chassis_port → triplet_pair)
    - Generate required OCS patches (full-mesh between chassis)
    - Compare against current OCS cross-connects
    - Return delta (patches to add / patches to remove)
    """
    port_map = _load_full_port_map()

    # Load chassis nodes
    chassis_nodes = list(
        topo.nodes.filter(pk__in=chassis_node_ids)
             .select_related('device')
    )
    switch_nodes = list(
        topo.nodes.filter(pk__in=switch_node_ids)
             .select_related('device')
    )
    ocs_node = topo.nodes.filter(node_type='ocs').select_related('device').first()

    # Build per-chassis data
    chassis_data = []
    for node in chassis_nodes:
        ip = ''
        if node.device:
            ip = node.device.ip_address or ''
        if not ip:
            # Try KeysightChassis lookup via chassis_id in extra
            cid = (node.extra or {}).get('chassis_id')
            if cid:
                try:
                    from .models import KeysightChassis
                    kc = KeysightChassis.objects.get(pk=cid)
                    ip = kc.ip_address or ''
                except Exception:
                    pass
        # Try ocs_triplet_map embedded directly in node.extra (from schema)
        embedded_triplets = (node.extra or {}).get('ocs_triplet_map', {})
        pm = port_map.get(ip, {})
        # Prefer embedded triplets over site JSON if available
        triplets = embedded_triplets if embedded_triplets else pm.get('port_to_ocs_triplets', {})
        chassis_data.append({
            'node_id': node.pk,
            'label': node.label,
            'ip': ip,
            'ocs_ip': pm.get('ocs_ip', '') or (node.extra or {}).get('ocs_ip', ''),
            'port_to_triplets': triplets,
        })

    switch_data = []
    for node in switch_nodes:
        ip = node.device.ip_address if node.device else ''
        embedded_triplets = (node.extra or {}).get('ocs_triplet_map', {})
        pm = port_map.get(ip, {})
        triplets = embedded_triplets if embedded_triplets else pm.get('port_to_ocs_triplets', {})
        switch_data.append({
            'node_id': node.pk,
            'label': node.label,
            'ip': ip,
            'ocs_ip': pm.get('ocs_ip', ''),
            'port_to_triplets': triplets,
        })

    # Build full port-connection table showing chassis_port → triplets → partner
    # For each chassis, list all its ports with OCS triplet mapping
    port_connections = []
    for cd in chassis_data:
        for port, triplets in sorted(cd['port_to_triplets'].items(),
                                     key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 0):
            port_connections.append({
                'chassis_label': cd['label'],
                'chassis_ip': cd['ip'],
                'port': port,
                'triplets': triplets,
                'ocs_ip': cd['ocs_ip'],
            })

    # Also show switch OCS connections (switch ports that connect via optic to OCS)
    switch_ocs_ports = []
    for sd in switch_data:
        for port, triplets in sorted(sd['port_to_triplets'].items(),
                                     key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 0):
            switch_ocs_ports.append({
                'switch_label': sd['label'],
                'switch_ip': sd['ip'],
                'port': port,
                'triplets': triplets,
            })

    # Generate required OCS patches: full-mesh between chassis
    required_patches = []
    for i, ca in enumerate(chassis_data):
        for j, cb in enumerate(chassis_data):
            if j <= i:
                continue
            ports_a = sorted(ca['port_to_triplets'].items(),
                             key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 0)
            ports_b = sorted(cb['port_to_triplets'].items(),
                             key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 0)
            for k, ((pa, ta), (pb, tb)) in enumerate(zip(ports_a, ports_b)):
                for idx, (ta_single, tb_single) in enumerate(zip(ta, tb)):
                    required_patches.append({
                        'chassis_a': ca['label'],
                        'chassis_a_ip': ca['ip'],
                        'port_a': pa,
                        'triplet_a': ta_single,
                        'chassis_b': cb['label'],
                        'chassis_b_ip': cb['ip'],
                        'port_b': pb,
                        'triplet_b': tb_single,
                        'xc_name': f'LV_{ca["ip"].split(".")[-1]}_{cb["ip"].split(".")[-1]}_{pa}_{idx}',
                    })

    # Get current OCS cross-connects
    current_patches: List[Dict] = []
    if ocs_node and ocs_node.device:
        current_patches = _get_current_ocs_xconnects(ocs_node.device)

    # Build triplet lookup sets for fast comparison
    current_triplet_pairs = set()
    for xc in current_patches:
        a = xc.get('port_a', '').strip()
        b = xc.get('port_b', '').strip()
        if a and b:
            current_triplet_pairs.add((min(a, b), max(a, b)))

    # Mark required patches as existing / missing
    all_required_triplets = set()
    for p in required_patches:
        ta, tb = p['triplet_a'], p['triplet_b']
        pair = (min(ta, tb), max(ta, tb))
        p['status'] = 'exists' if pair in current_triplet_pairs else 'missing'
        all_required_triplets.add(pair)

    # Find patches that exist but aren't needed (would need removal)
    # Only flag cross-connects involving our selected chassis triplets
    selected_triplets = set()
    for cd in chassis_data:
        for port, ts in cd['port_to_triplets'].items():
            selected_triplets.update(ts)

    extra_patches = []
    for xc in current_patches:
        a, b = xc.get('port_a', '').strip(), xc.get('port_b', '').strip()
        if (a in selected_triplets or b in selected_triplets):
            pair = (min(a, b), max(a, b))
            if pair not in all_required_triplets:
                extra_patches.append({**xc, 'status': 'extra'})

    needed_to_add = sum(1 for p in required_patches if p['status'] == 'missing')
    needed_to_remove = len(extra_patches)
    already_correct = sum(1 for p in required_patches if p['status'] == 'exists')

    return {
        'chassis': chassis_data,
        'switches': switch_data,
        'ocs': {
            'node_id': ocs_node.pk if ocs_node else None,
            'ip': ocs_node.device.ip_address if (ocs_node and ocs_node.device) else '',
            'label': ocs_node.label if ocs_node else 'OCS',
        },
        'port_connections': port_connections,
        'switch_ocs_ports': switch_ocs_ports,
        'required_patches': required_patches,
        'current_patches': current_patches[:200],
        'extra_patches': extra_patches,
        'summary': {
            'chassis_count': len(chassis_data),
            'switch_count': len(switch_data),
            'total_required': len(required_patches),
            'already_correct': already_correct,
            'to_add': needed_to_add,
            'to_remove': needed_to_remove,
            'port_pairs': len(port_connections),
        },
    }


@login_required
@require_http_methods(['GET'])
def lab_topology_scenario(request, topo_id):
    """
    Scenario analysis: current vs needed OCS patches for a test setup.

    GET params:
      chassis=<node_id1>,<node_id2>,...   — chassis node IDs
      switch=<node_id1>,<node_id2>,...    — switch node IDs (optional)
    """
    topo = get_object_or_404(LabTopology, pk=topo_id)
    chassis_ids = [int(x) for x in request.GET.get('chassis', '').split(',') if x.strip().isdigit()]
    switch_ids  = [int(x) for x in request.GET.get('switch', '').split(',') if x.strip().isdigit()]

    if not chassis_ids:
        return JsonResponse({'ok': False, 'error': 'chassis param required (comma-separated node IDs)'}, status=400)

    try:
        result = _compute_scenario(topo, chassis_ids, switch_ids)
        return JsonResponse({'ok': True, **result})
    except Exception as exc:
        logger.exception('lab_topology_scenario topo=%s', topo_id)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


@login_required
@require_http_methods(['GET'])
def lab_topology_portmap(request, topo_id):
    """Return the OCS port-map for all devices in the topology (for canvas enrichment)."""
    topo = get_object_or_404(LabTopology, pk=topo_id)
    port_map = _load_full_port_map()

    nodes_out = []
    for node in topo.nodes.select_related('device').all():
        ip = node.device.ip_address if node.device else ''
        pm = port_map.get(ip, {})
        nodes_out.append({
            'node_id': node.pk,
            'label': node.label,
            'node_type': node.node_type,
            'ip': ip,
            'ocs_ip': pm.get('ocs_ip', ''),
            'port_to_ocs_triplets': pm.get('port_to_ocs_triplets', {}),
            'vendor_type': pm.get('vendor_type', node.node_type),
        })

    return JsonResponse({'ok': True, 'nodes': nodes_out, 'port_map': port_map})


# Optional leftover LLDP text dumps (customer SKU: none shipped).
_LLDP_RAW_DIR = ''
_ARISTA_IP_TO_LLDP: dict = {}
# Hostname → IP overlays come from inventory, not a baked-in lab map.
_LLDP_HOSTNAME_TO_IP: dict = {}


def _lldp_hostname_aliases(name: str):
    """Yield lookup keys for LLDP system names (short hostname, normalized label)."""
    raw = (name or '').lower().strip().split('.')[0]
    if not raw:
        return
    yield raw
    compact = raw.replace('-', '').replace('_', '').replace(' ', '')
    if compact and compact != raw:
        yield compact


def _lldp_hostname_map_for_topo(topo: LabTopology) -> Dict[str, str]:
    """
    Hostname → IPv4 for chassis in this topology (static lab map + inventory).
    Used so sub-topologies only resolve neighbors present in the topo.
    """
    from .models import KeysightChassis

    out: Dict[str, str] = {}
    for key, val in _LLDP_HOSTNAME_TO_IP.items():
        if val:
            out[key] = val

    for n in topo.nodes.filter(node_type='chassis').select_related('device'):
        extra = n.extra or {}
        ip = (extra.get('device_ip') or '').strip()
        cid = extra.get('chassis_id')
        ch = KeysightChassis.objects.filter(pk=cid).first() if cid else None
        if ch and ch.ip_address:
            ip = ch.ip_address.strip()
        if not ip:
            ip = _chassis_name_to_ip_helper(n.label) or ''
        if not ip:
            continue
        for host in (
            (ch.hostname if ch else '') or '',
            n.label or '',
            (n.node_key or '').replace('aresone', 'aresone_'),
        ):
            for alias in _lldp_hostname_aliases(host):
                out[alias] = ip
    return out


def _build_fabric_ip_to_node_id(
    node_list: List,
    *,
    chassis_ip_map: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """IPv4/IPv6/hostname → fabric node id for nodes in this topology only."""
    from .ip_addressing import identity_address_keys, normalize_ip
    from .models import KeysightChassis

    out: Dict[str, str] = {}
    for n in node_list:
        node_id = f'node_{n.pk}'
        extra = dict(n.extra or {})
        ipv4 = (n.device.ip_address if n.device else '') or extra.get('device_ip') or ''
        if not ipv4 and n.node_type == 'chassis':
            cid = extra.get('chassis_id')
            if cid:
                row = KeysightChassis.objects.filter(pk=cid).only('ip_address').first()
                ipv4 = (row.ip_address if row else '') or ''
            if not ipv4:
                ipv4 = _chassis_name_to_ip_helper(n.label) or ''
                if not ipv4 and chassis_ip_map:
                    lbl = n.label.lower().replace('-', '').replace(' ', '').replace('_', '')
                    ipv4 = chassis_ip_map.get(lbl, '') or ''
        ipv6 = normalize_ip(extra.get('mgmt_ipv6') or '')
        label = (n.label or '').strip()
        for key in identity_address_keys(ipv4=ipv4, ipv6=ipv6, hostname=label):
            out[key] = node_id
        nk = (n.node_key or '').strip()
        if nk:
            out[nk] = node_id
        cid = extra.get('chassis_id')
        if cid:
            ch = KeysightChassis.objects.filter(pk=cid).only(
                'ip_address', 'hostname', 'mgmt_ipv6',
            ).first()
            if ch:
                for key in identity_address_keys(
                    ipv4=ch.ip_address or '',
                    ipv6=ch.mgmt_ipv6 or '',
                    hostname=ch.hostname or '',
                ):
                    out[key] = node_id
    return out


def _lldp_hostname_to_ip(
    hostname: str,
    host_map: Optional[Dict[str, str]] = None,
) -> str:
    """Resolve a chassis LLDP hostname to its management IP. Returns '' if unknown."""
    h = (hostname or '').lower().strip().split('.')[0]  # drop domain suffix
    if host_map and h in host_map:
        return host_map[h] or ''
    for alias in _lldp_hostname_aliases(h):
        if host_map and alias in host_map:
            return host_map[alias] or ''
    v = _LLDP_HOSTNAME_TO_IP.get(h)
    if v is None:
        for alias in _lldp_hostname_aliases(h):
            v = _LLDP_HOSTNAME_TO_IP.get(alias)
            if v is not None:
                break
    if v is None:
        return ''
    if v == '':
        return ''
    return v


def _sync_refresh_switch_lldp_for_topo(topo_id: int) -> int:
    """SSH LLDP refresh for switches in one topology (blocking, for Port Fabric Refresh)."""
    from django.core.management import call_command
    from .topology_graph import switch_ips_for_topology

    ips = switch_ips_for_topology(topo_id)
    if not ips:
        return 0
    call_command('refresh_lldp', ip=ips, topo_id=topo_id, verbosity=0)
    return len(ips)


def _parse_lldp_txt(fpath):
    """Parse 'show lldp table' text → {local_eth_port: {remote_device, remote_port, up:True}}."""
    import re as _re
    result = {}
    try:
        with open(fpath, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip()
                if not line or not line[0].isalpha():
                    continue
                parts = _re.split(r'\s{2,}', line.strip())
                if len(parts) >= 3 and parts[0].startswith('Ethernet'):
                    result[parts[0]] = {
                        'remote_device': parts[1].split('.')[0],  # short hostname
                        'remote_port':   parts[2],
                        'up': True,
                    }
    except (OSError, IOError):
        pass
    return result


def _load_switch_lldp(ip):
    """Return LLDP neighbors dict for a switch IP. Reads from saved file; no live SSH."""
    import os
    fname = _ARISTA_IP_TO_LLDP.get(ip, '')
    if not fname:
        return {}
    fpath = os.path.join(_LLDP_RAW_DIR, fname)
    if not os.path.isfile(fpath):
        return {}
    return _parse_lldp_txt(fpath)


def _eth_to_port_num(eth_name):
    """EthernetN → N (interface index)."""
    import re as _re
    m = _re.search(r'(\d+)$', eth_name or '')
    return int(m.group(1)) if m else -1


def _eth_lldp_neighbor_for_port(port_label: str, eth_lldp: Dict) -> Optional[Dict]:
    """Resolve Arista/Sonic Ethernet port label to an LLDP neighbor entry."""
    if not eth_lldp or not port_label:
        return None
    pl = port_label.strip()
    if pl in eth_lldp:
        return eth_lldp[pl]
    import re as _re
    m = _re.match(r'^ethernet(\d+)$', pl, _re.I)
    if m:
        ek = f'Ethernet{m.group(1)}'
        if ek in eth_lldp:
            return eth_lldp[ek]
    pn = _eth_to_port_num(pl) if pl.lower().startswith('ethernet') else -1
    if pn < 0:
        m2 = _re.search(r'(?:^port[_\s]*)?(\d+)$', pl, _re.I)
        pn = int(m2.group(1)) if m2 else -1
    if pn >= 0:
        ek = f'Ethernet{pn}'
        if ek in eth_lldp:
            return eth_lldp[ek]
        for ek, nbr in eth_lldp.items():
            if _eth_to_port_num(ek) == pn:
                return nbr
    return None


def _triplet_xcon_rows_from_map(triplet_to_xcon: Dict[str, Dict]) -> List[Dict]:
    """Convert port-fabric triplet_to_xcon dict → OCS crossconnect row list."""
    from . import ocs_helpers

    rows: List[Dict] = []
    seen: Set[Tuple[str, str]] = set()
    for ta, info in (triplet_to_xcon or {}).items():
        tb = ocs_helpers.norm_ocs_triplet_key(str(info.get('peer') or ''))
        a = ocs_helpers.norm_ocs_triplet_key(str(ta))
        if not a or not tb:
            continue
        pair = tuple(sorted([a, tb]))
        if pair in seen:
            continue
        seen.add(pair)
        rows.append({
            'port_a': a,
            'port_b': tb,
            'name': info.get('path_id', '') or f'{a}-{tb}',
        })
    return rows


def _load_ocs_physical_ports(ocs_device) -> List[Dict]:
    """Physical OCS port inventory from device poller cache (all panels)."""
    if not ocs_device:
        return []
    try:
        from .views import _get_cached_data, _get_stale_cached_data
        cached = _get_cached_data(ocs_device.id) or _get_stale_cached_data(ocs_device.id) or {}
        return list(cached.get('physical_data') or [])
    except Exception:
        return []


def _planned_port_matches(planned_key: str, port_label: str) -> bool:
    """Match topology planned keys (slot_1, slot_1,3,5) to site port labels (port_1, 1.1)."""
    import re as _re
    if not planned_key or not port_label:
        return False
    if planned_key == port_label:
        return True
    sm = _re.search(r'slot[_\s]*([\d,\s]+)', planned_key, _re.I)
    # slot_N is a resource group (RG), not the same as port_N (OCS-facing index).
    if sm and not _re.search(r'slot[_\s]*', port_label, _re.I):
        slots = [int(x) for x in _re.findall(r'\d+', sm.group(1))]
        dm = _re.match(r'^(\d+)\.(\d+)$', port_label)
        if dm:
            return int(dm.group(1)) in slots
    dm = _re.match(r'^(\d+)\.(\d+)$', port_label)
    if dm and sm:
        rg = int(dm.group(1))
        slots = [int(x) for x in _re.findall(r'\d+', sm.group(1))]
        return rg in slots
    return False


def _upgrade_dac_connections_from_port_lldp(
    devices_out: List[Dict],
    connections_out: List[Dict],
) -> None:
    """Promote DAC links to lldp_only when endpoint ports already show LLDP up."""
    dev_by_id = {d['id']: d for d in devices_out}
    port_by_id: Dict[str, Dict] = {}
    for d in devices_out:
        for p in _iter_fabric_ports(d):
            pid = p.get('id')
            if pid:
                port_by_id[pid] = p

    for conn in connections_out:
        if conn.get('health') not in ('dac', 'planned', 'unused'):
            continue
        upgraded = False
        for side in ('src', 'dst'):
            pid = conn.get(f'{side}_port') or ''
            port = port_by_id.get(pid)
            if not port:
                dev = dev_by_id.get(conn.get(f'{side}_device') or '')
                if dev:
                    port = _find_fabric_port(dev, pid)
            if not port:
                continue
            if port.get('health') == 'lldp_only' and port.get('link_up'):
                upgraded = True
                break
            if port.get('lldp_neighbor') or port.get('lldp_remote_device'):
                if port.get('link_up') or port.get('health') == 'lldp_only':
                    upgraded = True
                    break
        if upgraded:
            conn['health'] = 'lldp_only'
            conn['link_up'] = True
            conn['type'] = 'dac'
            conn['color'] = '#f59e0b'


def _upgrade_live_ocs_connections(
    connections_out: List[Dict],
    triplet_to_xcon: Dict[str, Dict],
) -> None:
    """Promote planned OCS links to active when live crossconnects exist."""
    import re as _re
    from . import ocs_helpers

    if not triplet_to_xcon:
        return
    for conn in connections_out:
        if conn.get('health') not in ('planned', 'unused', 'dac'):
            continue
        found = False
        for t in conn.get('ocs_triplets') or []:
            tk = ocs_helpers.norm_ocs_triplet_key(str(t))
            if tk in triplet_to_xcon:
                found = True
                break
        if not found:
            for side in ('src_port', 'dst_port'):
                ref = conn.get(side) or ''
                lbl = ref.split('__', 1)[-1]
                for part in _re.findall(r'\d+\.\d+\.\d+', lbl):
                    tk = ocs_helpers.norm_ocs_triplet_key(part)
                    if tk in triplet_to_xcon:
                        found = True
                        break
                if found:
                    break
        if found:
            conn['health'] = 'active'
            conn['link_up'] = True
            conn['type'] = 'ocs_active'
            conn['color'] = '#22c55e'


def _add_cached_topology_lldp_links(
    topo: LabTopology,
    devices_out: list,
    connections_out: list,
    conn_seen: set,
    summary: dict,
    lldp_by_eth: Optional[Dict] = None,
) -> None:
    """
    Import LLDP adjacency from get_cached_topology() (same source as Network Topology page).
    Switch LLDP files/DB may be empty; topology scan still populates ChassisDeviceLink.
    """
    from .topology import get_cached_topology, CHASSIS_NODE_PREFIX

    try:
        lldp_topo = get_cached_topology() or {}
    except Exception as exc:
        logger.debug('port_fabric: cached topology LLDP skipped: %s', exc)
        return

    ip_to_node = {d['ip']: d['id'] for d in devices_out if d.get('ip')}
    devid_to_node = {}
    chassisid_to_node = {}
    for n in topo.nodes.select_related('device'):
        if n.device_id:
            devid_to_node[int(n.device_id)] = f'node_{n.pk}'
        cid = (n.extra or {}).get('chassis_id')
        if cid:
            chassisid_to_node[int(cid)] = f'node_{n.pk}'
        if n.node_type == 'chassis' and n.label:
            ip = (n.extra or {}).get('device_ip') or _chassis_name_to_ip_helper(n.label)
            if ip:
                ip_to_node[ip] = f'node_{n.pk}'

    def _map_gid(gid: str) -> str:
        if gid.startswith(CHASSIS_NODE_PREFIX):
            try:
                ch_id = int(gid[len(CHASSIS_NODE_PREFIX):])
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
        if e.get('status') not in ('up', 'active', True):
            continue
        sid = _map_gid(str(e.get('source', '')))
        tid = _map_gid(str(e.get('target', '')))
        if not sid or not tid or sid == tid:
            continue
        ck = tuple(sorted([sid, tid]))
        if ck in conn_seen:
            continue
        conn_seen.add(ck)
        # Prefer switch → chassis direction for DAC fabric drawing
        sw_id, ch_id = sid, tid
        dev_s = next((d for d in devices_out if d['id'] == sid), None)
        dev_t = next((d for d in devices_out if d['id'] == tid), None)
        if dev_s and dev_s.get('node_type') == 'chassis':
            sw_id, ch_id = tid, sid
            dev_s, dev_t = dev_t, dev_s
        if dev_s and dev_s.get('node_type') != 'switch':
            sw_id, ch_id = tid, sid
        link_count = int(e.get('link_count') or 1)
        connections_out.append({
            'id': f'cached_lldp_{sw_id}_{ch_id}',
            'src_device': sw_id,
            'src_port': '',
            'dst_device': ch_id,
            'dst_port': '',
            'type': 'dac',
            'health': 'lldp_only',
            'link_up': True,
            'lldp_neighbor': dev_t.get('label', '') if dev_t else '',
            'color': '#f59e0b',
            'ocs_triplets': [],
            'loss_db': None,
            'label': f'LLDP ×{link_count}',
            'cable_type': 'dac',
            'lldp_port_count': link_count,
        })
        summary['lldp_only'] = summary.get('lldp_only', 0) + link_count

        # Mark switch DAC ports that have LLDP on this Arista/Sonic node
        sw_eth = (lldp_by_eth or {}).get(
            next((d.get('ip') for d in devices_out if d['id'] == sw_id), ''),
            {},
        )
        for d in devices_out:
            if d['id'] != sw_id or d.get('node_type') != 'switch':
                continue
            for grp in d.get('port_groups') or []:
                if grp.get('role') != 'dac':
                    continue
                for p in grp.get('ports') or []:
                    nbr = _eth_lldp_neighbor_for_port(p.get('label', ''), sw_eth)
                    if not nbr:
                        continue
                    from .topology_device_ports import ixos_link_is_down
                    if ixos_link_is_down(p.get('link_state', ''), p.get('led_color', '')):
                        continue
                    p['health'] = 'lldp_only'
                    p['link_up'] = True
                    p['oper_state'] = 'up'
                    p['lldp_remote_device'] = nbr.get('remote_device', '')
                    p['lldp_remote_port'] = nbr.get('remote_port', '')
                    p['health_color'] = '#f59e0b'
            break


def _find_fabric_port(dev: Dict, port_ref: str) -> Optional[Dict]:
    """Resolve port label, EthernetN, slot_N, or node_X__label to a fabric port entry."""
    import re as _re
    if not dev or not port_ref:
        return None
    label = port_ref.split('__', 1)[-1] if '__' in port_ref else port_ref
    eth_n = _eth_to_port_num(label) if label.lower().startswith('ethernet') else -1
    slot_nums: List[int] = []
    sm = _re.search(r'slot[_\s]*([\d,\s]+)', label, _re.I)
    if sm:
        slot_nums = [int(x) for x in _re.findall(r'\d+', sm.group(1))]
    for grp in dev.get('port_groups') or []:
        for p in grp.get('ports') or []:
            pid = p.get('id', '')
            plbl = p.get('label', '')
            if pid == port_ref or plbl == label or pid.endswith(f'__{label}'):
                return p
            if label in pid:
                return p
            if _planned_port_matches(label, plbl) or _planned_port_matches(plbl, label):
                return p
            if slot_nums and not _re.search(r'port[_\s]*', label, _re.I):
                dm = _re.match(r'^(\d+)\.', plbl)
                if dm and int(dm.group(1)) in slot_nums:
                    return p
            pm = _re.search(r'port[_\s]*(\d+)', label, _re.I)
            if pm and not slot_nums:
                want = int(pm.group(1))
                if (
                    plbl == label
                    or _re.search(rf'port[_\s]*{want}$', plbl, _re.I)
                    or p.get('index') == want
                ):
                    return p
            if eth_n >= 0 and p.get('index') == eth_n:
                return p
            if eth_n >= 0:
                for offset in (0, 8, 16, 32, 48, 64, 80, 96, 112, 128):
                    if p.get('index') == offset + eth_n:
                        return p
    return None


def _resolve_fabric_connection_ports(devices_out: List[Dict], connections_out: List[Dict]) -> None:
    """Attach src_port/dst_port IDs that exist on device port_groups."""
    dev_by_id = {d['id']: d for d in devices_out}

    for conn in connections_out:
        for side in ('src', 'dst'):
            dev_id = conn.get(f'{side}_device')
            port_ref = conn.get(f'{side}_port') or ''
            dev = dev_by_id.get(dev_id)
            if not dev:
                continue
            found = _find_fabric_port(dev, port_ref)
            if found:
                conn[f'{side}_port'] = found['id']
                continue
            if not port_ref and conn.get('lldp_port_count'):
                grp_role = 'dac' if dev.get('node_type') in ('switch', 'chassis') else 'ocs'
                for grp in dev.get('port_groups') or []:
                    if grp.get('role') != grp_role and dev.get('node_type') != 'ocs':
                        continue
                    for p in grp.get('ports') or []:
                        if p.get('link_up') or p.get('health') in ('lldp_only', 'active', 'dac'):
                            conn[f'{side}_port'] = p['id']
                            break
                    if conn.get(f'{side}_port'):
                        break


def _iter_fabric_ports(dev: Dict):
    """Yield every port dict on a fabric device (flat groups or OCS shelves)."""
    for shelf in dev.get('ocs_shelves') or []:
        for bank in shelf.get('banks') or []:
            for p in bank.get('ports') or []:
                yield p
    for grp in dev.get('port_groups') or []:
        for p in grp.get('ports') or []:
            yield p


def _propagate_connections_to_ports(devices_out, connections_out):
    """
    Apply each connection to its specific src/dst port (green=up, DAC/LLDP boxes).
    """
    from .topology_device_ports import fabric_link_type, fabric_oper_state

    HEALTH_COLOR = {
        'active': '#22c55e', 'alarm': '#ef4444', 'lldp_only': '#f59e0b',
        'planned': '#3b82f6', 'dac': '#8b5cf6', 'down': '#dc2626',
        'unused': '#1e293b', 'unknown': '#334155',
    }
    HP = {'alarm': 0, 'active': 1, 'lldp_only': 2, 'dac': 3, 'planned': 4, 'unused': 5}
    dev_by_id = {d['id']: d for d in devices_out}
    port_by_id: Dict[str, Dict] = {}
    port_by_dev_label: Dict[tuple, Dict] = {}

    for d in devices_out:
        for p in _iter_fabric_ports(d):
            pid = p.get('id', '')
            if pid:
                port_by_id[pid] = p
            lbl = p.get('label', '')
            if lbl:
                port_by_dev_label[(d['id'], lbl)] = p

    def _normalize_port_ref(port_ref) -> str:
        if not port_ref:
            return ''
        if isinstance(port_ref, dict):
            return str(port_ref.get('id') or port_ref.get('label') or port_ref.get('name') or '')
        return str(port_ref)

    def _find_port(dev_id: str, port_ref) -> Optional[Dict]:
        port_ref = _normalize_port_ref(port_ref)
        if not port_ref:
            return None
        p = port_by_id.get(port_ref)
        if p:
            return p
        lbl = port_ref.split('__', 1)[-1] if '__' in port_ref else port_ref
        p = port_by_dev_label.get((dev_id, lbl))
        if p:
            return p
        dev = dev_by_id.get(dev_id)
        if dev and lbl:
            for cand in _iter_fabric_ports(dev):
                cl = cand.get('label', '')
                if cl == lbl or _planned_port_matches(lbl, cl) or _planned_port_matches(cl, lbl):
                    return cand
            import re as _re_p
            pm = _re_p.search(r'port[_\s]*(\d+)', lbl, _re_p.I)
            if pm:
                want = int(pm.group(1))
                for cand in _iter_fabric_ports(dev):
                    if cand.get('index') == want:
                        return cand
                    cl = cand.get('label', '')
                    if _re_p.search(rf'(?:^|\.)(\d+)$', cl) and int(_re_p.search(rf'(?:^|\.)(\d+)$', cl).group(1)) == want:
                        return cand
        return _find_fabric_port(dev or {}, port_ref)

    def _apply(port: Dict, conn: Dict, peer_dev: str, peer_port) -> None:
        peer_port = _normalize_port_ref(peer_port)
        health = conn.get('health') or 'unused'
        if health == 'unused':
            return
        cable = conn.get('cable_type') or conn.get('type') or ''
        role = 'ocs' if cable in ('optic', 'ocs') or health == 'active' else 'dac'
        if health == 'lldp_only':
            role = 'dac'
        link_up = bool(
            conn.get('link_up')
            or health in ('active', 'lldp_only')
        )
        from .topology_device_ports import ixos_link_is_down

        has_lldp = bool(
            port.get('lldp_neighbor')
            or port.get('lldp_remote_device')
            or conn.get('lldp_neighbor')
            or health == 'lldp_only'
        )
        if ixos_link_is_down(port.get('link_state', ''), port.get('led_color', '')):
            if not (has_lldp and health == 'lldp_only'):
                return
        if port.get('oper_state') == 'down' and health in ('lldp_only', 'dac', 'planned'):
            if not (has_lldp and health == 'lldp_only'):
                return
        cur_h = port.get('health') or 'unused'
        if HP.get(health, 5) > HP.get(cur_h, 5):
            return
        port['health'] = health
        port['health_color'] = HEALTH_COLOR.get(health, HEALTH_COLOR['unused'])
        port['link_up'] = link_up
        port['role'] = role
        port['link_type'] = fabric_link_type(health=health, role=role)
        port['oper_state'] = fabric_oper_state(
            health=health, link_up=link_up, role=role,
        )
        if peer_dev:
            port['peer_device_id'] = peer_dev
        if peer_port:
            peer_p = port_by_id.get(peer_port) or _find_port(peer_dev, peer_port)
            if peer_p:
                port['peer_port_label'] = peer_p.get('label') or peer_p.get('name') or ''
                port['peer_port_id'] = peer_p.get('id') or peer_port
            else:
                port['peer_port_label'] = peer_port.rsplit('__', 1)[-1]
                port['peer_port_id'] = peer_port
        if health == 'lldp_only':
            port['lldp_neighbor'] = conn.get('lldp_neighbor', '')

    for conn in connections_out:
        health = conn.get('health') or 'unused'
        if health == 'unused':
            continue
        for side, other in (('src', 'dst'), ('dst', 'src')):
            dev_id = conn.get(f'{side}_device')
            port_ref = _normalize_port_ref(conn.get(f'{side}_port') or '')
            peer_dev = conn.get(f'{other}_device') or ''
            peer_port = _normalize_port_ref(conn.get(f'{other}_port') or '')
            port = _find_port(dev_id, port_ref)
            if port:
                _apply(port, conn, peer_dev, peer_port)


def _attach_lldp_chassis_ports(
    topo: LabTopology,
    devices_out: List[Dict],
    connections_out: List[Dict],
) -> None:
    """Set chassis dst_port on LLDP bundle connections using topology link port_a/b."""
    dev_by_id = {d['id']: d for d in devices_out}
    for conn in connections_out:
        if conn.get('health') != 'lldp_only' or conn.get('dst_port'):
            continue
        ch_id = conn.get('dst_device')
        sw_id = conn.get('src_device')
        ch_dev = dev_by_id.get(ch_id or '')
        sw_dev = dev_by_id.get(sw_id or '')
        if not ch_dev or ch_dev.get('node_type') != 'chassis':
            continue
        try:
            ch_pk = int(str(ch_id).replace('node_', ''))
            sw_pk = int(str(sw_id).replace('node_', ''))
        except (TypeError, ValueError):
            continue
        lk = topo.links.filter(node_a_id=sw_pk, node_b_id=ch_pk).first()
        if not lk:
            lk = topo.links.filter(node_a_id=ch_pk, node_b_id=sw_pk).first()
        if not lk:
            continue
        ch_port = lk.port_b if lk.node_a_id == sw_pk else lk.port_a
        if ch_port:
            ch_dev = dev_by_id.get(ch_id or '')
            resolved = _find_fabric_port(ch_dev, f'{ch_id}__{ch_port}') if ch_dev else None
            conn['dst_port'] = resolved['id'] if resolved else f'{ch_id}__{ch_port}'


def _enrich_ports_from_node_details(devices_out, topo) -> None:
    """Re-apply IxOS link_state from node port_details when slot row was not refreshed."""
    from .topology_device_ports import (
        HEALTH_COLOR,
        fabric_state_from_ixos_port,
    )

    node_by_fabric_id = {f'node_{n.pk}': n for n in topo.nodes.all()}

    for dev in devices_out:
        node = node_by_fabric_id.get(dev['id'])
        if not node or node.node_type != 'chassis':
            continue
        details_by_key: Dict[str, Dict] = {}
        for pd in (node.extra or {}).get('port_details') or []:
            if not isinstance(pd, dict):
                continue
            cn = pd.get('card_number')
            pn = pd.get('port_number')
            if cn is not None and pn is not None:
                details_by_key[f'c{int(cn)}p{int(pn)}'] = pd
            nm = pd.get('name', '')
            if nm and re.match(r'^port[_\s]*\d+', str(nm), re.I):
                details_by_key[nm] = pd
        for p in _iter_fabric_ports(dev):
            slot = p.get('slot')
            pn = p.get('index')
            pd = None
            if slot is not None and pn is not None:
                pd = details_by_key.get(f'c{int(slot)}p{int(pn)}')
            if not pd:
                continue
            health, oper, link_up, lt = fabric_state_from_ixos_port(
                {**pd, 'role': p.get('role', 'dac')},
                role=p.get('role', 'dac'),
            )
            p['health'] = health
            p['oper_state'] = oper
            p['link_up'] = link_up
            p['link_type'] = lt
            p['link_state'] = pd.get('link_state', '')
            p['led_color'] = pd.get('led_color', '')
            p['health_color'] = HEALTH_COLOR.get(health, HEALTH_COLOR['unused'])


def _load_chassis_lldp_neighbors(
    ch, *, force_refresh: bool = False,
) -> tuple[List[Dict], str]:
    """IxOS/KCOS chassis LLDP: persisted store, in-memory cache, or SSH on force_refresh.

    Returns (neighbors, error_message).
    """
    if not ch:
        return [], ''
    from .lldp_persistence import get_entity_neighbors, persist_entity_neighbors
    from .keysight_views import _get_cached

    err = ''
    if force_refresh:
        try:
            from .keysight_drivers import get_driver, KCOS_TYPES
            drv = get_driver(ch)
            is_kcos = (ch.chassis_type or '') in KCOS_TYPES
            # SSH first for every driver that supports it: IxOS "show lldp-peer-info",
            # KCOS root-SSH hop across compute-node interfaces.
            if hasattr(drv, 'get_lldp_ssh'):
                bps_topology = None
                if (ch.chassis_type or '') == 'aps_m8400':
                    try:
                        from .keysight_drivers.bps import BPSDriver
                        bps_res = BPSDriver(ch.ip_address, ch.username, ch.password).get_topology()
                        if bps_res.success and isinstance(bps_res.data, dict):
                            bps_topology = bps_res.data
                    except Exception:
                        pass
                res = drv.get_lldp_ssh(
                    bps_topology=bps_topology,
                    chassis_type=ch.chassis_type or '',
                )
                if res.success and res.data:
                    rows = persist_entity_neighbors(
                        'chassis', ch.id, list(res.data), fresh_scan_ok=True,
                    )
                    return rows, ''
                err = getattr(res, 'error', '') or getattr(drv, '_last_ssh_error', '') or ''
                if not err and not (res.data or []):
                    err = 'SSH LLDP returned no neighbors'
            if is_kcos:
                from .topology_lldp import fetch_chassis_lldp
                comm = (getattr(ch, 'snmp_community', None) or '').strip() or 'public'
                rows = fetch_chassis_lldp(ch.ip_address, community=comm, timeout=5)
                if rows:
                    rows = persist_entity_neighbors(
                        'chassis', ch.id, rows, fresh_scan_ok=True,
                    )
                    return rows, ''
        except Exception as exc:
            err = str(exc)
            logger.warning('port_fabric: chassis LLDP refresh for %s: %s', ch.ip_address, exc)

    cached = _get_cached(ch.id) or {}
    rows = list(cached.get('lldp_neighbors') or [])
    if rows:
        return rows, err
    persisted = get_entity_neighbors('chassis', ch.id)
    if persisted:
        return persisted, err
    return [], err


def _fabric_port_for_ixos_peer(dev: Dict, peer_port: str) -> str:
    """Resolve IxOS peer Port ID (e.g. 1/2, 3/1) to a fabric port id on the same chassis."""
    peer_port = (peer_port or '').strip()
    dev_id = dev.get('id', '')
    m = re.match(r'^(\d+)/(\d+)$', peer_port)
    if m:
        cn, pn = int(m.group(1)), int(m.group(2))
        for p in _iter_fabric_ports(dev):
            slot = p.get('slot')
            if slot is None:
                slot = p.get('card_number')
            idx = p.get('index')
            if idx is None:
                idx = p.get('port_number')
            if slot == cn and idx == pn:
                return p['id']
    found = _find_fabric_port(dev, f'{dev_id}__{peer_port}')
    if found:
        return found['id']
    for p in _iter_fabric_ports(dev):
        if (p.get('label') or '') == peer_port:
            return p['id']
    return ''


def _add_ixos_lldp_b2b_connections(
    devices_out: List[Dict],
    connections_out: List[Dict],
    conn_seen: set,
    summary: Dict,
) -> None:
    """B2B arcs from IxOS ``show lldp-peer-info`` when peer is the same chassis (loopback/DUT)."""
    for dev in devices_out:
        if dev.get('node_type') != 'chassis':
            continue
        chassis_ip = (dev.get('ip') or '').strip()
        host = (dev.get('label') or '').strip().lower().split('.')[0]
        for port in _iter_fabric_ports(dev):
            mgmt = (port.get('lldp_mgmt_ip') or '').strip()
            rd = (port.get('lldp_remote_device') or '').strip()
            rp = (port.get('lldp_remote_port') or '').strip()
            if not rp:
                continue
            rd_key = rd.lower().split('.')[0]
            same_chassis = bool(
                (mgmt and chassis_ip and mgmt == chassis_ip)
                or (rd_key and host and (rd_key == host or host in rd_key or rd_key in host))
            )
            if not same_chassis:
                continue
            dst_id = _fabric_port_for_ixos_peer(dev, rp)
            if not dst_id or dst_id == port.get('id'):
                continue
            ck = tuple(sorted([port['id'], dst_id]))
            if ck in conn_seen:
                continue
            conn_seen.add(ck)
            connections_out.append({
                'id': f'lldp_b2b_{dev["id"]}_{len(connections_out)}',
                'src_device': dev['id'],
                'src_port': port['id'],
                'dst_device': dev['id'],
                'dst_port': dst_id,
                'type': 'b2b',
                'health': 'lldp_only',
                'link_kind': 'b2b',
                'link_up': True,
                'color': '#3b82f6',
                'cable_type': 'direct',
                'label': f'B2B LLDP {port.get("label", "")}↔{rp}',
                'ocs_triplets': [],
                'loss_db': None,
                'b2b_front': port.get('label', ''),
                'b2b_back': rp,
            })
            summary['lldp_only'] = summary.get('lldp_only', 0) + 1


def _add_chassis_b2b_connections(
    topo: LabTopology,
    devices_out: List[Dict],
    connections_out: List[Dict],
    conn_seen: set,
    summary: Dict,
) -> None:
    """Emit same-chassis B2B links from topology DB + transceiver serial pairs."""
    from collections import defaultdict
    from .topology_dac_finder import collect_chassis_port_serials, normalize_serial
    from .topology_device_ports import ixos_link_is_down

    dev_by_id = {d['id']: d for d in devices_out}

    for lk in topo.links.select_related('node_a', 'node_b').all():
        extra = lk.extra or {}
        kind = extra.get('link_kind', '')
        if lk.cable_type != 'direct' and kind != 'b2b':
            continue
        if lk.node_a_id != lk.node_b_id:
            continue
        na, nb = f'node_{lk.node_a_id}', f'node_{lk.node_b_id}'
        sp = f'{na}__{lk.port_a}' if lk.port_a else ''
        dp = f'{nb}__{lk.port_b}' if lk.port_b else ''
        ck = tuple(sorted([sp or na, dp or nb]))
        if ck in conn_seen:
            continue
        conn_seen.add(ck)
        connections_out.append({
            'id': f'b2b_{lk.pk}',
            'link_id': lk.pk,
            'src_device': na,
            'src_port': sp,
            'dst_device': nb,
            'dst_port': dp,
            'type': 'b2b',
            'health': 'lldp_only',
            'link_kind': 'b2b',
            'link_up': True,
            'color': '#3b82f6',
            'cable_type': 'direct',
            'label': lk.label or 'B2B',
            'ocs_triplets': [],
            'loss_db': None,
        })
        summary['lldp_only'] = summary.get('lldp_only', 0) + 1

    for dev in devices_out:
        if dev.get('node_type') != 'chassis':
            continue
        cid = dev.get('chassis_id')
        if not cid:
            continue
        from .models import KeysightChassis
        ch = KeysightChassis.objects.filter(pk=cid).first()
        if not ch:
            continue
        entries = collect_chassis_port_serials(ch, refresh=False)
        by_serial: Dict[str, List] = defaultdict(list)
        for e in entries:
            sn = normalize_serial(e.get('serial'))
            if sn:
                by_serial[sn].append(e)

        def _port_id_for_entry(e: Dict) -> str:
            port_lbl = e.get('port', '')
            dev_id = dev['id']
            found = _find_fabric_port(dev, f'{dev_id}__{port_lbl}')
            if found:
                return found['id']
            for p in _iter_fabric_ports(dev):
                if p.get('label') == port_lbl or p.get('label') == port_lbl.split('p')[-1]:
                    return p['id']
            m = re.search(r'c(\d+)p(\d+)', port_lbl, re.I)
            if m:
                for p in _iter_fabric_ports(dev):
                    if p.get('slot') == int(m.group(1)) and p.get('index') == int(m.group(2)):
                        return p['id']
            return f'{dev_id}__{port_lbl}'

        for _serial, group in by_serial.items():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    pa = _port_id_for_entry(group[i])
                    pb = _port_id_for_entry(group[j])
                    ck = tuple(sorted([pa, pb]))
                    if ck in conn_seen:
                        continue
                    p_a = next((p for p in _iter_fabric_ports(dev) if p['id'] == pa), None)
                    p_b = next((p for p in _iter_fabric_ports(dev) if p['id'] == pb), None)
                    if p_a and ixos_link_is_down(p_a.get('link_state', ''), p_a.get('led_color', '')):
                        continue
                    if p_b and ixos_link_is_down(p_b.get('link_state', ''), p_b.get('led_color', '')):
                        continue
                    conn_seen.add(ck)
                    connections_out.append({
                        'id': f'b2b_serial_{dev["id"]}_{len(connections_out)}',
                        'src_device': dev['id'],
                        'src_port': pa,
                        'dst_device': dev['id'],
                        'dst_port': pb,
                        'type': 'b2b',
                        'health': 'lldp_only',
                        'link_kind': 'b2b',
                        'link_up': True,
                        'color': '#3b82f6',
                        'cable_type': 'direct',
                        'label': f'B2B serial {_serial[:10]}',
                        'ocs_triplets': [],
                        'loss_db': None,
                    })
                    summary['lldp_only'] = summary.get('lldp_only', 0) + 1


# ── Port-fabric build cache (site JSON + API response) ─────────────────────────
_SITE_BUNDLE_CACHE: Dict[str, Any] = {'sig': None, 'bundle': None}
_TRIPLET_MAP_CACHE: Dict[Any, Any] = {}


def _site_json_paths():
    from pathlib import Path as _Path
    _SITE_DIR = _Path(__file__).parent.parent / 'resources'
    return sorted(_SITE_DIR.glob('ocs_photonic_site*.json'))


def _site_json_signature() -> tuple:
    sig = []
    for p in _site_json_paths():
        try:
            sig.append((str(p), p.stat().st_mtime))
        except OSError:
            pass
    return tuple(sig)


def _load_site_port_bundle():
    """Parse all ocs_photonic_site*.json once; invalidate when any file mtime changes."""
    from collections import defaultdict
    import json as _json, re as _re

    sig = _site_json_signature()
    if _SITE_BUNDLE_CACHE.get('sig') == sig and _SITE_BUNDLE_CACHE.get('bundle'):
        return _SITE_BUNDLE_CACHE['bundle']

    site_port_data = defaultdict(dict)
    chassis_ip_map = {}
    for _sf in _site_json_paths():
        try:
            sd = _json.loads(_sf.read_text())
            for sw in (sd.get('arista_switches') or []):
                sip = sw.get('ip', '')
                fm = (sw.get('fixed_mapping') or {}).get('port_to_ocs_triplets') or {}
                for pl, ts in fm.items():
                    if not isinstance(ts, list):
                        ts = [ts]
                    site_port_data[sip][pl] = {'triplets': [str(t) for t in ts], 'role': 'ocs'}
            for ch in (sd.get('keysight_chassis') or []):
                chip = ch.get('ip', '')
                fm = (ch.get('fixed_mapping') or {}).get('port_to_ocs_triplets') or {}
                if not fm:
                    notes = ch.get('notes', '')
                    m = _re.search(r'\[ocs_site_mapping\]\s*(\{.+\})', notes, _re.DOTALL)
                    if m:
                        try:
                            fm = _json.loads(m.group(1)).get('port_to_ocs_triplets') or {}
                        except Exception:
                            pass
                for pl, ts in fm.items():
                    if not isinstance(ts, list):
                        ts = [ts]
                    site_port_data[chip][pl] = {'triplets': [str(t) for t in ts], 'role': 'ocs'}
                _nm = (ch.get('name') or '').lower().replace('_', '').replace('-', '').replace(' ', '')
                if _nm and chip:
                    chassis_ip_map[_nm] = chip
        except Exception as exc:
            logger.debug('port_fabric site JSON error: %s', exc)

    bundle = {'site_port_data': site_port_data, 'chassis_ip_map': chassis_ip_map}
    _SITE_BUNDLE_CACHE['sig'] = sig
    _SITE_BUNDLE_CACHE['bundle'] = bundle
    return bundle


def _cached_triplet_map(ocs_ip: str, all_dev) -> Dict[str, Any]:
    sig = _site_json_signature()
    key = (ocs_ip or '', sig, len(all_dev))
    if key in _TRIPLET_MAP_CACHE:
        return _TRIPLET_MAP_CACHE[key]
    m = ocs_helpers.build_ocs_triplet_map(ocs_ip, all_dev) if ocs_ip else {}
    _TRIPLET_MAP_CACHE[key] = m
    return m


def _build_port_fabric_payload(
    topo,
    *,
    want_live: bool,
    want_lldp: bool,
    force_refresh: bool,
    reload_switch_lldp: bool = False,
    profile: bool = True,
) -> tuple:
    """
    Build port-fabric JSON (devices, connections, summary). Used by API + cache warmer.
    Returns (payload_dict, timing_dict).
    """
    import time as _perf
    from datetime import datetime, timezone as _tz
    from collections import defaultdict
    import json as _json, re as _re
    from pathlib import Path as _Path
    from django.core.cache import cache
    from django.db.models import Prefetch

    _t0 = _perf.perf_counter()
    _timing: Dict[str, float] = {}

    # ── 1. Build full OCS triplet map via proven helper ────────────────────────
    # Returns: {triplet_key → {ip, hostname, sw_port, device_id}}
    _t_ocs_map = _perf.perf_counter()
    ocs_node = next((n for n in topo.nodes.all() if n.node_type == 'ocs'), None)
    ocs_ip = ocs_node.device.ip_address if (ocs_node and ocs_node.device) else ''
    all_dev = list(Device.objects.only('id', 'ip_address', 'hostname', 'notes'))
    triplet_map = _cached_triplet_map(ocs_ip, all_dev)
    _timing['triplet_map_ms'] = round((_perf.perf_counter() - _t_ocs_map) * 1000, 1)

    # Build inverse: ip + port_label → [triplets that share that port]
    # Since multiple triplets map to the same physical port (tx/rx pair), group them
    ip_port_to_triplets = defaultdict(list)  # (ip, sw_port) → [triplet_key, ...]
    for tri, info in triplet_map.items():
        k = (info.get('ip', ''), info.get('sw_port', ''))
        if k[0] and k[1]:
            ip_port_to_triplets[k].append(tri)

    # ── 2. Live OCS crossconnects ──────────────────────────────────────────────
    # triplet_key → {peer, health, loss_db, path_id}
    triplet_to_xcon = {}
    ocs_error = ''
    ocs_summary = {'total': 0, 'active': 0, 'alarm': 0}
    ocs_xcons_for_shelves: List[Dict] = []
    ocs_physical_data: List[Dict] = _load_ocs_physical_ports(
        ocs_node.device if ocs_node else None,
    )
    _ocs_xcon_cache_key = f'port_fabric_ocs_xcon:{ocs_ip}' if ocs_ip else ''
    if _ocs_xcon_cache_key:
        triplet_to_xcon = dict(cache.get(_ocs_xcon_cache_key) or {})
    _t_ocs_live = _perf.perf_counter()
    if want_live and ocs_node and ocs_node.device:
        try:
            from .drivers import get_driver
            drv = get_driver(ocs_node.device)
            rows = drv.fetch_crossconnect_list()
            xcons = drv.get_ocs_crossconnects(raw_rows=rows).data or []
            ocs_xcons_for_shelves = list(xcons)
            try:
                ifr = drv.get_interfaces()
                if ifr and ifr.success and (ifr.data or {}).get('physical_data'):
                    ocs_physical_data = list((ifr.data or {}).get('physical_data') or [])
            except Exception as exc_if:
                logger.debug('port_fabric: OCS physical ports: %s', exc_if)
            fresh_xcon: Dict[str, Dict] = {}
            _ALARM_CODES = {'CR', 'MJ', 'MN'}
            ocs_summary['total'] = len(xcons)
            for xc in xcons:
                ta = ocs_helpers.norm_ocs_triplet_key(xc.get('port_a', ''))
                tb = ocs_helpers.norm_ocs_triplet_key(xc.get('port_b', ''))
                h2 = xc.get('h2', {})
                alarm = h2.get('alarm', 'CL') in _ALARM_CODES or h2.get('oc', 'OK') not in ('OK', '')
                # Match OCS device page: patched triplets show green (active), not alarm-red per port
                health = 'active'
                if alarm:
                    ocs_summary['alarm'] += 1
                else:
                    ocs_summary['active'] += 1
                loss = h2.get('loss')
                try:
                    loss = float(loss) if loss not in (None, '') else None
                    if loss is not None and loss < -80:
                        loss = None
                except (ValueError, TypeError):
                    loss = None
                path_id = xc.get('name', f'{ta}-{tb}')
                for t, peer in [(ta, tb), (tb, ta)]:
                    fresh_xcon[t] = {'peer': peer, 'health': health,
                                    'loss_db': loss, 'path_id': path_id}
            if fresh_xcon:
                triplet_to_xcon = fresh_xcon
                if _ocs_xcon_cache_key:
                    cache.set(_ocs_xcon_cache_key, triplet_to_xcon, 600)
        except Exception as exc:
            logger.warning('port_fabric: OCS fetch failed: %s', exc)
            ocs_error = str(exc)
    elif ocs_xcons_for_shelves == [] and triplet_to_xcon:
        ocs_xcons_for_shelves = _triplet_xcon_rows_from_map(triplet_to_xcon)
    _timing['ocs_live_ms'] = round((_perf.perf_counter() - _t_ocs_live) * 1000, 1)

    # ── 3. LLDP: cache file (from refresh_lldp command) + raw files ──────────
    # lldp_by_eth[ip] = {eth_port: {remote_device, remote_port, up}}  (from files)
    lldp_by_eth = {}   # keyed by switch IP
    lldp_by_ip_port = {}  # (ip, port_label) → neighbor_str
    _t_lldp = _perf.perf_counter()
    if want_lldp:
        from .lldp_persistence import load_switch_lldp_by_ip, LLDP_RETENTION_SECONDS
        import time as _time
        # A. 24h persistent switch LLDP store
        try:
            for _ip, nbr_list in load_switch_lldp_by_ip().items():
                eth_dict = {}
                for nbr in nbr_list:
                    lp = nbr.get('local_port', '')
                    if lp.startswith('Ethernet'):
                        eth_dict[lp] = {
                            'remote_device': nbr.get('remote_device', ''),
                            'remote_port': nbr.get('remote_port', ''),
                            'up': True,
                        }
                if eth_dict:
                    lldp_by_eth[_ip] = eth_dict
        except Exception as _e:
            logger.debug('port_fabric: persistent LLDP read error: %s', _e)

        # B. From aggregated cache file (written by refresh_lldp management command)
        _LLDP_CACHE_FILE = _Path('/tmp/labvault_lldp_cache.json')
        if _LLDP_CACHE_FILE.is_file():
            try:
                _cache = _json.loads(_LLDP_CACHE_FILE.read_text())
                _age = _time.time() - _cache.get('fetched_at', 0)
                if _age < LLDP_RETENTION_SECONDS:
                    for _ip, _dc in _cache.get('devices', {}).items():
                        if _ip in lldp_by_eth:
                            continue
                        eth_dict = {}
                        for nbr in (_dc.get('neighbors') or []):
                            lp = nbr.get('local_port', '')
                            if lp.startswith('Ethernet'):
                                eth_dict[lp] = {
                                    'remote_device': nbr.get('remote_device', ''),
                                    'remote_port': nbr.get('remote_port', ''),
                                    'up': True,
                                }
                        if eth_dict:
                            lldp_by_eth[_ip] = eth_dict
            except Exception as _e:
                logger.debug('port_fabric: LLDP cache read error: %s', _e)

        # C. From saved LLDP raw files — only switches present in this topology
        from .topology_graph import switch_ips_for_topology

        topo_switch_ips = set(switch_ips_for_topology(topo.pk))
        for sw_ip in topo_switch_ips:
            if sw_ip in lldp_by_eth and not reload_switch_lldp:
                continue
            if sw_ip in _ARISTA_IP_TO_LLDP:
                lldp_data = _load_switch_lldp(sw_ip)
                if lldp_data:
                    lldp_by_eth[sw_ip] = lldp_data

        # D. From LabVault topology cache (may be empty)
        try:
            from .topology import get_cached_topology
            tc = get_cached_topology() or {}
            for e in (tc.get('links') or tc.get('edges') or []):
                for src_ip, src_port, dst_ip, dst_port in [
                    (e.get('source_ip') or e.get('source', ''),
                     e.get('local_port') or e.get('source_port', ''),
                     e.get('target_ip') or e.get('target', ''),
                     e.get('remote_port') or e.get('target_port', '')),
                    (e.get('target_ip') or e.get('target', ''),
                     e.get('remote_port') or e.get('target_port', ''),
                     e.get('source_ip') or e.get('source', ''),
                     e.get('local_port') or e.get('source_port', '')),
                ]:
                    if src_ip and src_port:
                        lldp_by_ip_port[(src_ip, src_port)] = f'{dst_ip}:{dst_port}'
        except Exception:
            pass
    _timing['lldp_ms'] = round((_perf.perf_counter() - _t_lldp) * 1000, 1)

    # ── 4. Site JSON (cached in-process) ───────────────────────────────────────
    _t_site = _perf.perf_counter()
    _bundle = _load_site_port_bundle()
    _site_port_data = _bundle['site_port_data']
    _chassis_ip_map = _bundle['chassis_ip_map']
    _timing['site_json_ms'] = round((_perf.perf_counter() - _t_site) * 1000, 1)

    lldp_host_map = _lldp_hostname_map_for_topo(topo) if want_lldp else {}

    def resolve_ip_for_node(n):
        ip = n.device.ip_address if n.device else (n.extra or {}).get('device_ip', '')
        if ip:
            return ip
        if n.node_type == 'chassis':
            cid = (n.extra or {}).get('chassis_id')
            if cid:
                _ch_row = KeysightChassis.objects.filter(pk=cid).only(
                    'ip_address', 'hostname',
                ).first()
                if _ch_row and _ch_row.ip_address:
                    return _ch_row.ip_address
            lbl = n.label.lower().replace('-','').replace(' ','').replace('_','')
            for k, v in _chassis_ip_map.items():
                km = _re.search(r'(\d+)$', k)
                lm = _re.search(r'(\d+)$', lbl)
                if km and lm and int(km.group(1)) == int(lm.group(1)) and k[:4] == lbl[:4]:
                    return v
        return ''

    # ── 6. Build devices + ports ───────────────────────────────────────────────
    HEALTH_COLOR = {
        'active':   '#22c55e',
        'alarm':    '#ef4444',
        'lldp_only':'#f59e0b',
        'planned':  '#3b82f6',
        'dac':      '#8b5cf6',
        'down':     '#dc2626',
        'unused':   '#1e293b',
        'unknown':  '#334155',
    }

    summary = {'total_ports': 0, 'active': 0, 'alarm': 0, 'lldp_only': 0,
               'planned': 0, 'dac': 0, 'down': 0, 'unused': 0}

    node_list = list(topo.nodes.select_related('device').all())
    link_between = {}
    for lk in topo.links.all():
        link_between[(lk.node_a_id, lk.node_b_id)] = lk
        link_between[(lk.node_b_id, lk.node_a_id)] = lk
    # IPv4/IPv6/hostname keys — scoped to nodes in this topology (critical for sub-topologies)
    ip_to_node_id = _build_fabric_ip_to_node_id(
        node_list, chassis_ip_map=_chassis_ip_map,
    )
    ip_to_node_type: Dict[str, str] = {}
    for _n in node_list:
        _nip = resolve_ip_for_node(_n)
        if _nip:
            ip_to_node_type[_nip] = _n.node_type

    devices_out = []
    connections_out = []
    conn_seen = set()

    from .topology_device_ports import fabric_link_type, fabric_oper_state

    # Build planned-link lookup per node
    planned_by_node = defaultdict(list)
    for lk in topo.links.all():
        lk_extra = lk.extra or {}
        planned_by_node[lk.node_a_id].append({
            'peer_node_id': f'node_{lk.node_b_id}',
            'my_port': lk.port_a or '', 'peer_port': lk.port_b or '',
            'cable_type': lk.cable_type or 'dac', 'label': lk.label or lk.cable_type or 'DAC',
            'link_pk': lk.pk,
            'link_kind': lk_extra.get('link_kind', ''),
            'link_extra': lk_extra,
        })
        planned_by_node[lk.node_b_id].append({
            'peer_node_id': f'node_{lk.node_a_id}',
            'my_port': lk.port_b or '', 'peer_port': lk.port_a or '',
            'cable_type': lk.cable_type or 'dac', 'label': lk.label or lk.cable_type or 'DAC',
            'link_pk': lk.pk,
            'link_kind': lk_extra.get('link_kind', ''),
            'link_extra': lk_extra,
        })

    _t_devices = _perf.perf_counter()
    for n in node_list:
        ch_lldp_rows: List[Dict] = []
        ch_lldp_err = ''
        node_id = f'node_{n.pk}'
        ip = resolve_ip_for_node(n)
        site_ports = _site_port_data.get(ip, {})  # port_label → {triplets, role}
        planned_links = {pl['my_port']: pl for pl in planned_by_node[n.pk]}

        def _portnum(p):
            m = _re.search(r'(\d+)$', p)
            return int(m.group(1)) if m else 0

        # All OCS ports from site JSON (proven source)
        ocs_port_labels = sorted(site_ports.keys(), key=_portnum)

        # Extra ports from topology DB (non-OCS, e.g. DAC ranges)
        extra_ports_raw = list((n.extra or {}).get('ports') or [])
        extra_port_labels = []
        for ep in extra_ports_raw:
            ep_label = ep if isinstance(ep, str) else (ep.get('label') or ep.get('id') or '')
            if ep_label and ep_label not in site_ports:
                extra_port_labels.append(ep_label)

        ocs_ports = []
        dac_ports = []

        # Build LLDP lookup for this device:
        # a) from file-based eth LLDP (for Arista switches)
        eth_lldp = lldp_by_eth.get(ip, {})
        # Summary: which remote devices are seen via LLDP from this switch
        lldp_remote_summary = {}  # remote_device → [eth_port, ...]
        for eth_p, nbr in eth_lldp.items():
            rd = nbr.get('remote_device', '')
            if rd:
                lldp_remote_summary.setdefault(rd, []).append(eth_p)
        # ETH port index list for DAC-connected devices (for matching to port groups)
        lldp_eth_indices = {_eth_to_port_num(ep): nbr for ep, nbr in eth_lldp.items()}

        # Include LLDP-seen Ethernet ports in fabric (DAC to chassis), not only topo extra list
        if n.node_type == 'switch' and eth_lldp:
            for eth_p in eth_lldp.keys():
                if eth_p not in site_ports and eth_p not in extra_port_labels:
                    extra_port_labels.append(eth_p)
            extra_port_labels = sorted(set(extra_port_labels), key=_portnum)

        def build_port_entry(pl, role, *, fabric_label=None, single_triplet=None):
            sp = site_ports.get(pl, {})
            triplets = [single_triplet] if single_triplet else sp.get('triplets', [])
            fabric_lbl = fabric_label or pl
            port_health = 'unused'
            ocs_triplet = ''
            peer_node_id = ''
            peer_port_label = ''
            loss_db = None
            lldp_neighbor = lldp_by_ip_port.get((ip, pl), '')
            ocs_path_id = ''
            link_up = False
            lldp_remote_device = ''
            lldp_remote_port = ''

            # Check live OCS for each triplet
            for t in triplets:
                tk = ocs_helpers.norm_ocs_triplet_key(t)
                if tk in triplet_to_xcon:
                    xc = triplet_to_xcon[tk]
                    port_health = xc['health']
                    ocs_triplet = tk
                    loss_db = xc.get('loss_db')
                    ocs_path_id = xc.get('path_id', '')
                    link_up = (port_health == 'active')
                    peer_t = ocs_helpers.norm_ocs_triplet_key(xc.get('peer', ''))
                    peer_info = triplet_map.get(peer_t, {})
                    peer_ip = peer_info.get('ip', '')
                    from .topology_device_ports import fanout_peer_port_label
                    peer_port_label = fanout_peer_port_label(
                        peer_t,
                        peer_ip,
                        site_port_data=_site_port_data,
                        peer_node_type=ip_to_node_type.get(peer_ip, ''),
                    ) if peer_ip else peer_info.get('sw_port', '')
                    peer_node_id = ip_to_node_id.get(peer_ip, '')
                    break

            # LLDP from switch cache/files — match exact Ethernet label first
            if eth_lldp and not lldp_remote_device:
                nbr_eth = _eth_lldp_neighbor_for_port(pl, eth_lldp)
                if nbr_eth:
                    lldp_remote_device = nbr_eth.get('remote_device', '')
                    lldp_remote_port = nbr_eth.get('remote_port', '')
                    lldp_neighbor = f"{lldp_remote_device}:{lldp_remote_port}"
                    link_up = True
                    if port_health == 'unused':
                        port_health = 'lldp_only'
                elif role == 'dac':
                    import re as _re2
                    pnum_local = _portnum(pl)
                    for offset in (0, 8, 16, 32, 48, 64, 80, 96, 112, 128):
                        eth_idx = offset + pnum_local
                        if eth_idx in lldp_eth_indices:
                            nbr = lldp_eth_indices[eth_idx]
                            lldp_remote_device = nbr.get('remote_device', '')
                            lldp_remote_port = nbr.get('remote_port', '')
                            lldp_neighbor = f"{lldp_remote_device}:{lldp_remote_port}"
                            link_up = True
                            if port_health == 'unused':
                                port_health = 'lldp_only'
                            break

            # Cache/topology LLDP fallback
            if not lldp_neighbor:
                lldp_neighbor = lldp_by_ip_port.get((ip, pl), '')
                if lldp_neighbor and port_health == 'unused':
                    port_health = 'lldp_only'
                    link_up = True

            # Planned link fallback (slot_N ↔ port_N on chassis)
            if port_health == 'unused':
                for pkey, plink in planned_links.items():
                    if pkey == pl or _planned_port_matches(pkey, pl):
                        port_health = 'planned'
                        if plink.get('cable_type') in ('dac', 'direct'):
                            port_health = 'dac'
                        peer_node_id = plink['peer_node_id']
                        peer_port_label = plink['peer_port']
                        break

            if role == 'dac' and port_health == 'unused':
                for pkey, plink in planned_links.items():
                    if plink['cable_type'] in ('dac', 'direct'):
                        port_health = 'dac'
                        break

            summary['total_ports'] += 1
            hk = port_health if port_health in summary else 'unused'
            summary[hk] += 1

            pnum = _portnum(pl)
            peer_port_id = f'{peer_node_id}__{peer_port_label}' if peer_node_id and peer_port_label else ''
            link_type = fabric_link_type(health=port_health, role=role)
            oper_state = fabric_oper_state(health=port_health, link_up=link_up, role=role)

            return {
                'id': f'{node_id}__{fabric_lbl}',
                'label': fabric_lbl,
                'index': pnum,
                'speed': '400G' if role == 'ocs' else '100G',
                'role': role,
                'link_type': link_type,
                'oper_state': oper_state,
                'health': port_health,
                'link_up': link_up,
                'health_color': HEALTH_COLOR.get(port_health, HEALTH_COLOR['unused']),
                'ocs_triplet': ocs_triplet,
                'ocs_path_id': ocs_path_id,
                'peer_device_id': peer_node_id,
                'peer_port_id': peer_port_id,
                'peer_port_label': peer_port_label,
                'lldp_neighbor': lldp_neighbor,
                'lldp_remote_device': lldp_remote_device,
                'lldp_remote_port': lldp_remote_port,
                'loss_db': loss_db,
            }

        # Build OCS-facing ports (fanout-aligned with designer: 1.1, 2.1.1, …)
        from .topology_device_ports import iter_ocs_fanout_site_ports

        topo_port_filter = None
        if extra_ports_raw and n.node_type in ('chassis', 'switch'):
            if any(_re.match(r'^\d+\.\d', str(x)) for x in extra_ports_raw):
                topo_port_filter = {str(x) for x in extra_ports_raw}

        if n.node_type in ('chassis', 'switch') and site_ports:
            ocs_iter = iter_ocs_fanout_site_ports(site_ports, n.node_type)
        else:
            ocs_iter = ((pl, site_ports.get(pl, {}), pl) for pl in ocs_port_labels)

        for fabric_lbl, sp, logical_pl in ocs_iter:
            if topo_port_filter and fabric_lbl not in topo_port_filter and logical_pl not in topo_port_filter:
                continue
            trips = sp.get('triplets') or []
            single_t = str(trips[0]) if trips else None
            entry = build_port_entry(
                logical_pl,
                'ocs',
                fabric_label=fabric_lbl,
                single_triplet=single_t,
            )
            ocs_ports.append(entry)
            # Emit connection
            if entry['peer_device_id'] and entry['peer_port_id']:
                ck = tuple(sorted([entry['id'], entry['peer_port_id']]))
                if ck not in conn_seen:
                    conn_seen.add(ck)
                    connections_out.append({
                        'id': f'conn_{len(connections_out)}',
                        'src_device': node_id, 'src_port': entry['id'],
                        'dst_device': entry['peer_device_id'], 'dst_port': entry['peer_port_id'],
                        'type': 'ocs_active' if entry['health'] in ('active', 'alarm') else 'planned',
                        'health': entry['health'],
                        'link_up': entry['link_up'],
                        'lldp_neighbor': entry['lldp_neighbor'],
                        'color': HEALTH_COLOR.get(entry['health'], '#475569'),
                        'ocs_triplets': [entry['ocs_triplet']],
                        'loss_db': entry['loss_db'],
                        'label': entry['ocs_path_id'] or entry['ocs_triplet'],
                        'cable_type': 'optic',
                    })

        # Build DAC/extra ports
        for pl in extra_port_labels:
            entry = build_port_entry(pl, 'dac')
            dac_ports.append(entry)
            if entry['peer_device_id'] and entry['peer_port_id']:
                ck = tuple(sorted([entry['id'], entry['peer_port_id']]))
                if ck not in conn_seen:
                    conn_seen.add(ck)
                    connections_out.append({
                        'id': f'conn_{len(connections_out)}',
                        'src_device': node_id, 'src_port': entry['id'],
                        'dst_device': entry['peer_device_id'], 'dst_port': entry['peer_port_id'],
                        'type': 'dac', 'health': 'dac',
                        'link_up': entry['link_up'],
                        'lldp_neighbor': entry['lldp_neighbor'],
                        'color': HEALTH_COLOR['dac'],
                        'ocs_triplets': [], 'loss_db': None, 'label': 'DAC',
                        'cable_type': 'dac',
                    })

        # Planned links that aren't covered by any port above
        for plink in planned_by_node[n.pk]:
            if plink['link_pk'] and not any(
                    c.get('id') == f'plan_{plink["link_pk"]}' for c in connections_out):
                my_port_id = f'{node_id}__{plink["my_port"]}' if plink['my_port'] else ''
                peer_port_id = f'{plink["peer_node_id"]}__{plink["peer_port"]}' if plink['peer_port'] else ''
                ck = tuple(sorted([my_port_id or node_id, peer_port_id or plink['peer_node_id']]))
                if ck not in conn_seen:
                    conn_seen.add(ck)
                    lk_kind = plink.get('link_kind') or ''
                    ct = plink.get('cable_type') or 'dac'
                    if lk_kind == 'b2b' or ct == 'direct':
                        conn_type, conn_health = 'b2b', 'lldp_only' if lk_kind else 'planned'
                        conn_color = '#3b82f6'
                    else:
                        conn_type, conn_health = 'dac', 'planned'
                        conn_color = HEALTH_COLOR['planned']
                    connections_out.append({
                        'id': f'plan_{plink["link_pk"]}',
                        'link_id': plink['link_pk'],
                        'src_device': node_id, 'src_port': my_port_id,
                        'dst_device': plink['peer_node_id'], 'dst_port': peer_port_id,
                        'type': conn_type, 'health': conn_health,
                        'link_kind': lk_kind or ('b2b' if ct == 'direct' else 'dac'),
                        'color': conn_color,
                        'ocs_triplets': [], 'loss_db': None,
                        'label': plink['label'], 'cable_type': ct,
                    })

        # Build port_groups — same slot layout as topology designer
        from .topology_device_ports import (
            apply_ocs_live_to_port_groups,
            build_fabric_health_lookup,
            build_node_slot_layout,
            chassis_fabric_port_groups,
            compact_fabric_port_groups,
            ocs_health_by_triplet,
            ocs_shelves_from_device_shelves,
            ocs_shelves_from_slot_layout,
            slot_layout_to_port_groups,
        )

        extra_n = n.extra or {}
        health_by_name, health_by_index = build_fabric_health_lookup(
            ocs_ports, dac_ports, extra_n.get('port_details'),
        )
        saved_slots = extra_n.get('slot_layout')
        ocs_shelves = None

        if n.node_type == 'ocs':
            health_by_name = ocs_health_by_triplet(triplet_to_xcon, triplet_map)
            site_t = ocs_helpers.load_ocs_site_triplets(ocs_ip)
            if ocs_physical_data or ocs_xcons_for_shelves or triplet_to_xcon:
                raw_shelves, _ocs_flat = ocs_helpers.build_ocs_shelves(
                    ocs_physical_data,
                    ocs_xcons_for_shelves or _triplet_xcon_rows_from_map(triplet_to_xcon),
                    [],
                    ocs_ip,
                    triplet_map,
                )
                raw_shelves = ocs_helpers.ensure_ocs_shelf_panels(raw_shelves, site_t)
                ocs_shelves = ocs_shelves_from_device_shelves(
                    raw_shelves, node_id, health_by_name, health_by_index,
                )
            else:
                ocs_shelves = None
            layout = build_node_slot_layout(
                n, topo,
                site_port_data=_site_port_data,
                triplet_to_xcon=triplet_to_xcon,
                refresh=force_refresh,
                ocs_physical_ports=ocs_physical_data,
            )
            slots = layout.get('slots') or saved_slots or []
            if not ocs_shelves:
                ocs_shelves = ocs_shelves_from_slot_layout(
                    node_id, slots, health_by_name, health_by_index,
                )
            port_groups = []
            if not ocs_shelves:
                ocs_active_ports = []
                for path_id, xc_grp in _group_xcons_by_blade(triplet_to_xcon).items():
                    ocs_active_ports.append({
                        'id': f'{node_id}__{path_id}',
                        'label': path_id, 'index': 0, 'speed': 'optical',
                        'role': 'ocs', 'health': xc_grp['health'],
                        'health_color': HEALTH_COLOR.get(xc_grp['health'], HEALTH_COLOR['unused']),
                        'ocs_triplet': path_id, 'ocs_path_id': path_id,
                        'peer_device_id': '', 'peer_port_id': '', 'peer_port_label': '',
                        'lldp_neighbor': '', 'loss_db': xc_grp.get('loss_db'),
                    })
                port_groups = [{'label': f'OCS ({len(ocs_active_ports)} active paths)',
                                'role': 'ocs', 'ports': ocs_active_ports}]
        elif n.node_type == 'chassis':
            from .topology_device_ports import (
                build_ixos_link_index,
                merge_ixos_link_state_into_slots,
                merge_lldp_into_fabric_port_groups,
            )

            layout = build_node_slot_layout(
                n, topo,
                site_port_data=_site_port_data,
                triplet_to_xcon=triplet_to_xcon,
                refresh=force_refresh,
            )
            slots = layout.get('slots') or saved_slots or []
            cid = extra_n.get('chassis_id')
            ch = KeysightChassis.objects.filter(pk=cid).first() if cid else None
            if not ch and ip:
                ch = KeysightChassis.objects.filter(ip_address=ip).first()
            if ch:
                link_index = build_ixos_link_index(ch, refresh=force_refresh)
                slots = merge_ixos_link_state_into_slots(slots, link_index)
            # Stale flat port_details must not override per-slot link_state.
            health_by_name, health_by_index = {}, {}
            port_groups = chassis_fabric_port_groups(
                node_id,
                slots=slots,
                ocs_ports=ocs_ports,
                dac_ports=dac_ports,
                chassis_type=extra_n.get('chassis_type', ''),
                health_by_name=health_by_name,
                health_by_index=health_by_index,
            )
            if ch and want_lldp:
                ch_lldp_rows, ch_lldp_err = _load_chassis_lldp_neighbors(
                    ch, force_refresh=force_refresh,
                )
                merge_lldp_into_fabric_port_groups(port_groups, ch_lldp_rows)
            apply_ocs_live_to_port_groups(
                port_groups, triplet_to_xcon, triplet_map,
                ip_to_node_id, _site_port_data, ip or '', node_id,
                ip_to_node_type=ip_to_node_type,
            )
        elif n.node_type in ('switch', 'firewall'):
            layout = build_node_slot_layout(
                n, topo,
                site_port_data=_site_port_data,
                triplet_to_xcon=triplet_to_xcon,
                refresh=force_refresh,
                additional_switch_ports=extra_port_labels,
            )
            fresh_slots = layout.get('slots') or []
            saved = saved_slots or []

            def _slot_port_count(slots_list):
                cnt = 0
                for sl in slots_list or []:
                    for rg in sl.get('resource_groups') or []:
                        cnt += len(rg.get('ports') or [])
                    cnt += len(sl.get('ports') or [])
                return cnt

            if saved and fresh_slots:
                slots = (
                    fresh_slots
                    if _slot_port_count(fresh_slots) >= _slot_port_count(saved)
                    else saved
                )
            else:
                slots = fresh_slots or saved
            if slots:
                port_groups = slot_layout_to_port_groups(
                    node_id, slots, health_by_name, health_by_index,
                )
            else:
                port_groups = []
                if ocs_ports:
                    port_groups.append({'label': 'OCS Uplink Ports', 'role': 'ocs', 'ports': ocs_ports})
                if dac_ports:
                    port_groups.append({'label': 'DAC / Direct Ports', 'role': 'dac', 'ports': dac_ports})
            apply_ocs_live_to_port_groups(
                port_groups, triplet_to_xcon, triplet_map,
                ip_to_node_id, _site_port_data, ip or '', node_id,
                ip_to_node_type=ip_to_node_type,
            )
        elif saved_slots:
            port_groups = slot_layout_to_port_groups(
                node_id, saved_slots, health_by_name, health_by_index,
            )
        else:
            layout = build_node_slot_layout(
                n, topo,
                site_port_data=_site_port_data,
                triplet_to_xcon=triplet_to_xcon,
                refresh=force_refresh,
            )
            if layout.get('slots'):
                port_groups = slot_layout_to_port_groups(
                    node_id, layout['slots'], health_by_name, health_by_index,
                )
            else:
                port_groups = []
                if ocs_ports:
                    port_groups.append({'label': 'OCS-Facing Ports', 'role': 'ocs', 'ports': ocs_ports})
                if dac_ports:
                    port_groups.append({'label': 'DAC / Direct Ports', 'role': 'dac', 'ports': dac_ports})

        port_groups = compact_fabric_port_groups(n.node_type, port_groups)

        _dev_lldp = ch_lldp_rows if n.node_type == 'chassis' else []
        _dev_lldp_err = ch_lldp_err if n.node_type == 'chassis' else ''
        devices_out.append({
            'id': node_id,
            'db_pk': n.pk,
            'chassis_id': extra_n.get('chassis_id'),
            'chassis_type': extra_n.get('chassis_type', ''),
            'label': n.label,
            'ip': ip or '',
            'mgmt_display': extra_n.get('mgmt_display') or '',
            'mgmt_ipv6': extra_n.get('mgmt_ipv6') or '',
            'mgmt_ipv4': extra_n.get('mgmt_ipv4') or ip or '',
            'preferred_ip_version': extra_n.get('preferred_ip_version') or '',
            'node_type': n.node_type,
            'vendor': (n.device.vendor_type if n.device else '') or n.node_type,
            'x': n.x,
            'y': n.y,
            'ocs_shelves': ocs_shelves,
            'port_groups': port_groups,
            'port_count': len(ocs_ports) + len(dac_ports),
            'lldp_neighbors': _dev_lldp,
            'lldp_fetch_error': _dev_lldp_err,
            'lldp_summary': lldp_remote_summary,  # {remote_device → [eth_port,...]}
            'lldp_dac_up': sum(1 for v in (ocs_ports + dac_ports) if v.get('link_up') and v.get('role') == 'dac'),
            'ocs_active_count': (
                sum(
                    1 for sh in (ocs_shelves or [])
                    for b in (sh.get('banks') or [])
                    for p in (b.get('ports') or [])
                    if p.get('health') == 'active'
                )
                if ocs_shelves
                else sum(1 for v in ocs_ports if v.get('health') == 'active')
            ),
            'ocs_alarm_count': (
                sum(
                    1 for sh in (ocs_shelves or [])
                    for b in (sh.get('banks') or [])
                    for p in (b.get('ports') or [])
                    if p.get('health') == 'alarm'
                )
                if ocs_shelves
                else sum(1 for v in ocs_ports if v.get('health') == 'alarm')
            ),
        })

    # ── Post-loop: build LLDP-verified DAC connections from switch LLDP files ───
    # For each switch, scan its lldp_by_eth and resolve remote chassis to topology nodes.
    # This is the only way to show M01-M04 connections (they have no OCS triplets).
    for dev in devices_out:
        sw_ip = dev['ip']
        if dev['node_type'] != 'switch' or not sw_ip:
            continue
        eth_lldp_sw = lldp_by_eth.get(sw_ip, {})
        if not eth_lldp_sw:
            continue
        # Build count per remote_device for this switch
        remote_counts = {}
        remote_sample_ports = {}
        for eth_p, nbr in eth_lldp_sw.items():
            rd = (nbr.get('remote_device') or '').lower().strip().split('.')[0]
            if not rd or rd in ('sonic', 'lbjpmlabasw01', ''):
                continue
            remote_counts[rd] = remote_counts.get(rd, 0) + 1
            if rd not in remote_sample_ports:
                remote_sample_ports[rd] = []
            remote_sample_ports[rd].append(eth_p)

        # Also mark this switch's own DAC ports as lldp_only for verified neighbors
        # (the Arista side: Ethernet80-87 to M06, Ethernet64-71 to M02, etc.)
        sw_lldp_verified_count = sum(
            1 for rd, nbr in eth_lldp_sw.items()
            if (nbr.get('remote_device') or '').lower() not in ('sonic', 'lbjpmlabasw01', '')
        )
        if sw_lldp_verified_count:
            for sw_dev in devices_out:
                if sw_dev['id'] != dev['id']:
                    continue
                marked = 0
                for grp in (sw_dev.get('port_groups') or []):
                    if grp.get('role') != 'dac':
                        continue
                    for p in grp.get('ports') or []:
                        pl = p.get('label', '')
                        nbr = _eth_lldp_neighbor_for_port(pl, eth_lldp_sw)
                        if not nbr:
                            continue
                        rd = (nbr.get('remote_device') or '').lower()
                        if not rd or rd in ('sonic', 'lbjpmlabasw01', ''):
                            continue
                        from .topology_device_ports import ixos_link_is_down
                        if ixos_link_is_down(p.get('link_state', ''), p.get('led_color', '')):
                            if p.get('health') not in ('down', 'unused'):
                                continue
                        p['health'] = 'lldp_only'
                        p['link_up'] = True
                        p['oper_state'] = 'up'
                        p['link_state'] = p.get('link_state') or 'up'
                        p['lldp_remote_device'] = nbr.get('remote_device', '')
                        p['lldp_remote_port'] = nbr.get('remote_port', '')
                        p['lldp_neighbor'] = (
                            f"{p['lldp_remote_device']}:{p['lldp_remote_port']}"
                        )
                        p['health_color'] = '#f59e0b'
                        marked += 1
                if marked:
                    summary['unused'] = max(0, summary.get('unused', 0) - marked)
                    summary['lldp_only'] = summary.get('lldp_only', 0) + marked
                break

        for rd_raw, count in remote_counts.items():
            # Resolve to IP (topology-scoped hostname map for sub-topologies)
            chassis_ip = _lldp_hostname_to_ip(rd_raw, lldp_host_map)
            if not chassis_ip:
                # Store unresolved LLDP neighbors in device summary for display
                if rd_raw and rd_raw not in ('sonic', 'lbjpmlabasw01'):
                    # Add to device's lldp_summary as unresolved
                    for d2 in devices_out:
                        if d2['id'] == dev['id']:
                            d2.setdefault('lldp_unresolved', {})[rd_raw] = count
                            break
                continue
            chassis_node_id = ip_to_node_id.get(chassis_ip, '')
            if not chassis_node_id:
                for _n in node_list:
                    if resolve_ip_for_node(_n) == chassis_ip:
                        chassis_node_id = f'node_{_n.pk}'
                        break
            if not chassis_node_id:
                continue
            ck = tuple(sorted([dev['id'], chassis_node_id]))
            if ck not in conn_seen:
                conn_seen.add(ck)
                sample_eth = remote_sample_ports.get(rd_raw, [])[:2]
                lldp_label = f'DAC LLDP ×{count}'
                dst_port = ''
                for chassis_dev in devices_out:
                    if chassis_dev['id'] != chassis_node_id:
                        continue
                    try:
                        ch_pk = int(str(chassis_node_id).replace('node_', ''))
                        sw_pk = int(str(dev.get('db_pk') or dev['id'].replace('node_', '')))
                    except (TypeError, ValueError):
                        ch_pk = sw_pk = 0
                    lk = link_between.get((sw_pk, ch_pk))
                    if lk:
                        ch_port = lk.port_b if lk.node_b_id == ch_pk else lk.port_a
                        if ch_port:
                            found = _find_fabric_port(
                                chassis_dev, f'{chassis_node_id}__{ch_port}',
                            )
                            if found:
                                dst_port = found['id']
                    if not dst_port:
                        for grp in chassis_dev.get('port_groups') or []:
                            if grp.get('role') != 'dac':
                                continue
                            for p in grp.get('ports') or []:
                                dst_port = p['id']
                                break
                            if dst_port:
                                break
                    break
                connections_out.append({
                    'id': f'lldp_dac_{dev["id"]}_{chassis_node_id}',
                    'src_device': dev['id'],
                    'src_port': f'{dev["id"]}__{sample_eth[0]}' if sample_eth else '',
                    'dst_device': chassis_node_id,
                    'dst_port': dst_port,
                    'type': 'dac',
                    'health': 'lldp_only',
                    'link_up': True,
                    'lldp_neighbor': rd_raw,
                    'color': '#f59e0b',   # amber = LLDP-verified DAC
                    'ocs_triplets': [],
                    'loss_db': None,
                    'label': lldp_label,
                    'cable_type': 'dac',
                    'lldp_port_count': count,
                })
                # Update summary
                summary['lldp_only'] = summary.get('lldp_only', 0) + count

    _timing['devices_ms'] = round((_perf.perf_counter() - _t_devices) * 1000, 1)

    _t_post = _perf.perf_counter()
    if want_lldp:
        _add_cached_topology_lldp_links(
            topo, devices_out, connections_out, conn_seen, summary, lldp_by_eth=lldp_by_eth,
        )

    _add_ixos_lldp_b2b_connections(devices_out, connections_out, conn_seen, summary)
    _add_chassis_b2b_connections(topo, devices_out, connections_out, conn_seen, summary)

    _upgrade_live_ocs_connections(connections_out, triplet_to_xcon)
    _upgrade_dac_connections_from_port_lldp(devices_out, connections_out)
    _resolve_fabric_connection_ports(devices_out, connections_out)
    _attach_lldp_chassis_ports(topo, devices_out, connections_out)
    _resolve_fabric_connection_ports(devices_out, connections_out)
    _propagate_connections_to_ports(devices_out, connections_out)
    _enrich_ports_from_node_details(devices_out, topo)

    _timing['post_ms'] = round((_perf.perf_counter() - _t_post) * 1000, 1)
    _timing['total_ms'] = round((_perf.perf_counter() - _t0) * 1000, 1)

    topo_extra = dict(topo.extra or {})
    payload = {
        'devices': devices_out,
        'connections': connections_out,
        'summary': summary,
        'ocs_summary': ocs_summary,
        'ocs_error': ocs_error,
        'fetched_at': datetime.now(_tz.utc).isoformat(),
        'topo_id': topo.pk,
        'topo_name': topo.name,
        'view_layouts': topo_extra.get('view_layouts') or {},
    }
    if profile:
        payload['_timing'] = _timing
    return payload, _timing


def refresh_port_fabric_snapshot(
    topo_id: int,
    *,
    want_live: bool = True,
    want_lldp: bool = True,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Rebuild and persist port-fabric snapshot (API + management command)."""
    from django.db.models import Prefetch
    from .topology_fabric_cache import (
        compute_topology_revision,
        invalidate_topology_fabric_cache,
        store_port_fabric_cached,
    )

    if force_refresh:
        invalidate_topology_fabric_cache(topo_id)

    topo = LabTopology.objects.prefetch_related(
        Prefetch('nodes', queryset=LabTopologyNode.objects.select_related('device')),
        Prefetch('links', queryset=LabTopologyLink.objects.select_related('node_a', 'node_b')),
    ).get(pk=topo_id)
    revision = compute_topology_revision(topo)
    payload, timing = _build_port_fabric_payload(
        topo,
        want_live=want_live,
        want_lldp=want_lldp,
        force_refresh=bool(force_refresh and want_live),
        profile=True,
    )
    timing['cache'] = 'rebuild'
    store_port_fabric_cached(
        topo_id,
        revision,
        payload,
        want_live=want_live,
        want_lldp=want_lldp,
        build_ms=timing.get('total_ms', 0),
        timing=timing,
    )
    return payload


@login_required
@require_http_methods(['GET'])
def lab_port_fabric_api(request, topo_id):
    """
    Port-level fabric map: every device with every port, connections at port granularity.

    Uses ocs_helpers.build_ocs_triplet_map (proven, site-JSON-backed) as primary source.

    Query params:
      live=1  — fetch live OCS crossconnects (default 1)
      lldp=1  — include LLDP adjacency (default 1)
      force_refresh=1 — bypass caches + refresh IxOS chassis cache (SSH)

    Response:
      {devices, connections, summary, fetched_at, _timing?}
    """
    import time as _perf
    from django.db.models import Prefetch
    from .topology_fabric_cache import (
        compute_topology_revision,
        get_port_fabric_cached,
        invalidate_topology_fabric_cache,
        schedule_port_fabric_refresh,
        store_port_fabric_cached,
    )

    _t0 = _perf.perf_counter()

    topo = get_object_or_404(
        LabTopology.objects.prefetch_related(
            Prefetch(
                'nodes',
                queryset=LabTopologyNode.objects.select_related('device'),
            ),
            Prefetch(
                'links',
                queryset=LabTopologyLink.objects.select_related('node_a', 'node_b'),
            ),
        ),
        pk=topo_id,
    )
    want_live = request.GET.get('live', '1') != '0'
    want_lldp = request.GET.get('lldp', '1') != '0'
    force_refresh = request.GET.get('force_refresh') == '1'
    profile = request.GET.get('profile', '1') == '1'
    revision = compute_topology_revision(topo)

    if force_refresh:
        invalidate_topology_fabric_cache(topo_id)
        try:
            n_sw = _sync_refresh_switch_lldp_for_topo(topo_id)
            if n_sw:
                logger.info('port_fabric: refreshed LLDP on %s switch(es) for topo %s', n_sw, topo_id)
        except Exception as exc:
            logger.warning('port_fabric: switch LLDP refresh failed topo %s: %s', topo_id, exc)
        # Chassis IxOS SSH is slow; warm cache in background without blocking Arista LLDP response.
        schedule_port_fabric_refresh(topo_id, want_live=want_live, chassis_ssh=False)

    cached, layer = get_port_fabric_cached(
        topo_id,
        revision,
        want_live=want_live,
        want_lldp=want_lldp,
        force_refresh=force_refresh,
    )
    if cached is not None and not force_refresh:
        if profile:
            cached = dict(cached)
            cache_meta = dict(cached.pop('_cache', {}) or {})
            cached['_timing'] = {
                'total_ms': round((_perf.perf_counter() - _t0) * 1000, 1),
                'cache': layer,
                'snapshot_age_s': cache_meta.get('age_s'),
            }
        resp = JsonResponse(cached)
        resp['X-Port-Fabric-Cache'] = layer
        if layer == 'db_stale' and want_live:
            schedule_port_fabric_refresh(topo_id, want_live=True)
        return resp

    # Synchronous path: live OCS + fresh switch LLDP; never block on chassis SSH.
    payload, timing = _build_port_fabric_payload(
        topo,
        want_live=want_live,
        want_lldp=want_lldp,
        force_refresh=False,
        reload_switch_lldp=force_refresh,
        profile=profile,
    )
    timing['cache'] = 'miss'
    if force_refresh:
        timing['switch_lldp_refresh'] = 'sync'
        timing['chassis_refresh'] = 'background_optional'
    store_port_fabric_cached(
        topo_id,
        revision,
        payload,
        want_live=want_live,
        want_lldp=want_lldp,
        build_ms=timing.get('total_ms', 0),
        timing=timing,
    )

    resp = JsonResponse(payload)
    resp['X-Port-Fabric-Cache'] = 'miss'
    if force_refresh:
        resp['X-Port-Fabric-Chassis-Refresh'] = 'background'
    if profile and payload.get('_timing'):
        timing = payload['_timing']
        resp['Server-Timing'] = '; '.join(
            f'{k.replace("_ms", "")};dur={v}' for k, v in timing.items()
            if k.endswith('_ms') and isinstance(v, (int, float))
        )
    return resp


def _chassis_name_to_ip_helper(label: str) -> str:
    """Public helper: resolve chassis label → IP from site JSON. Used by views.py."""
    import re as _re2, json as _json2
    from pathlib import Path as _Path2
    _site_dir = _Path2(__file__).parent.parent / 'resources'
    for _sf in sorted(_site_dir.glob('ocs_photonic_site*.json')):
        try:
            _sd = _json2.loads(_sf.read_text())
            for _kc in (_sd.get('keysight_chassis') or []):
                _nm = (_kc.get('name') or '').lower().replace('_', '').replace('-', '').replace(' ', '')
                _lbl = label.lower().replace('-', '').replace(' ', '').replace('_', '')
                _km = _re2.search(r'(\d+)$', _nm)
                _lm = _re2.search(r'(\d+)$', _lbl)
                if _km and _lm and int(_km.group(1)) == int(_lm.group(1)) and _nm[:4] == _lbl[:4]:
                    return _kc.get('ip', '')
        except Exception:
            pass
    return ''


def _group_xcons_by_blade(triplet_to_xcon):
    """Aggregate OCS crossconnects by path_id for OCS node display."""
    paths = {}
    for t, xc in triplet_to_xcon.items():
        pid = xc.get('path_id', t)
        if pid not in paths:
            paths[pid] = xc
    return paths


@login_required
@require_http_methods(['GET'])
def lab_port_fabric_page(request, topo_id):
    """Port-level fabric visualization page."""
    from connect.lab_topology_split import get_sub_topology_nav

    topo = get_object_or_404(LabTopology, pk=topo_id)
    return render(request, 'connect/lab_port_fabric.html', {
        'topo': topo,
        'sub_nav': get_sub_topology_nav(topo),
    })


@login_required
@require_http_methods(['GET'])
def lab_topology_usage_page(request, topo_id):
    """Topology & Utilization — 31-day cyclic port usage map (LabVault primary UI)."""
    from connect.lab_topology_split import get_sub_topology_nav

    topo = get_object_or_404(LabTopology, pk=topo_id)
    return render(request, 'connect/lab_topology_usage.html', {
        'topo': topo,
        'sub_nav': get_sub_topology_nav(topo),
    })


@login_required
@require_http_methods(['GET'])
def lab_topology_usage_graph_json(request, topo_id):
    """LabPortUsageGraphV1 — graph + reservations + utilization cycles (topology-scoped, cached)."""
    from django.core.cache import cache

    from .lab_timeline_views import _fabric_cache_key
    from .port_usage_graph import LabPortUsageGraphBuilder

    topo = get_object_or_404(LabTopology, pk=topo_id)
    cache_key = _fabric_cache_key(topo_id, request).replace('lab_fabric_graph:', 'lab_usage_graph:')
    cached = cache.get(cache_key)
    if cached is not None:
        return JsonResponse(cached)

    want_live = request.GET.get('live', '1') != '0'
    want_lldp = request.GET.get('lldp', '1') != '0'
    want_ocs = request.GET.get('ocs', '1') != '0'
    want_planned = request.GET.get('planned', '1') != '0'
    want_res = request.GET.get('reservations', '1') != '0'
    try:
        graph = LabPortUsageGraphBuilder(topo_id).build(
            live=want_live,
            lldp=want_lldp,
            ocs=want_ocs,
            planned=want_planned,
            reservations=want_res,
        )
    except Exception as exc:
        logger.exception('usage-graph.json failed for topo %s', topo_id)
        graph = {
            'schema_version': 1,
            'topology_id': topo_id,
            'ok': False,
            'error': 'Usage graph temporarily unavailable.',
            'devices': [],
            'port_nodes': [],
            'port_links': [],
            'nodes': [],
            'meta': {
                'stats': {},
                'metrics_collection_enabled': topo.metrics_collection_enabled,
            },
        }
    else:
        meta = graph.setdefault('meta', {})
        if isinstance(meta, dict):
            meta['metrics_collection_enabled'] = topo.metrics_collection_enabled
    cache.set(cache_key, graph, 90)
    return JsonResponse(graph)


@login_required
@require_http_methods(['GET'])
def lab_topology_usage_by_name(request, name):
    """Redirect to usage page for topology resolved by LabTopology.name."""
    from .topology_resolve import resolve_topology_by_labname

    topo, err = resolve_topology_by_labname(name)
    if err:
        return JsonResponse(err, status=int(err.get('status', 400)))
    return redirect('lab_topology_usage', topo_id=topo.pk)


@_api_auth_required
@require_http_methods(['POST'])
def api_port_usage_episodes(request):
    """
    Ingest LAAS run/reservation episodes into np_timeseries PortUsageSample.

    Body: { "episodes": [ { topology_id, episode_id, event, resource_key, ... } ] }
    or a single episode object. Use action=close to end an episode.
    """
    from .port_usage import ingest_episode_batch

    try:
        body = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest(json.dumps({'error': 'Invalid JSON'}), content_type='application/json')

    items = body.get('episodes')
    if items is None:
        items = [body]
    if not isinstance(items, list):
        return HttpResponseBadRequest(json.dumps({'error': 'episodes must be a list'}), content_type='application/json')

    results = ingest_episode_batch(items)
    ok = sum(1 for r in results if r.get('ok'))
    return JsonResponse({'ok': ok, 'total': len(results), 'results': results})


def lab_topology_laas_manifest(request, topo_id):
    """LAAS manifest export is hard-dumped from the customer SKU."""
    return JsonResponse({'error': 'not_available'}, status=404)


def lab_port_fabric_summary_api(request, topo_id):
    """LaaS/B2B fabric summary is hard-dumped from the customer SKU."""
    return JsonResponse({'error': 'not_available'}, status=404)


@_api_auth_required
@require_http_methods(['GET'])
def lab_topology_by_name(request, name):
    """Resolve topology id by exact LabTopology.name; 409 when ambiguous."""
    from .topology_resolve import resolve_topology_by_labname

    topo, err = resolve_topology_by_labname(name)
    if err:
        return JsonResponse(err, status=int(err.get('status', 400)))
    return JsonResponse({
        'topology_id': topo.pk,
        'topology_name': topo.name,
        'updated_at': topo.updated_at.isoformat() if topo.updated_at else None,
    })


@_api_auth_required
@require_http_methods(['GET'])
def lab_topology_list_json(request):
    """Machine-readable topology inventory for the LAAS lab catalog (additive)."""
    rows = []
    for topo in LabTopology.objects.all().order_by('pk'):
        node_kinds = {}
        for n in topo.nodes.all():
            node_kinds[n.node_type] = node_kinds.get(n.node_type, 0) + 1
        rows.append({
            'topology_id': topo.pk,
            'name': topo.name,
            'updated_at': topo.updated_at.isoformat() if topo.updated_at else None,
            'node_count': sum(node_kinds.values()),
            'node_kinds': node_kinds,
            'has_ocs': bool(node_kinds.get('ocs')),
            'has_dut': bool(node_kinds.get('switch')),
        })
    return JsonResponse({'topologies': rows, 'count': len(rows)})


@login_required
@require_http_methods(['GET'])
def lab_topology_ports_available(request, topo_id):
    """List ports not used by any planned link — for wire mode availability colors."""
    topo = get_object_or_404(LabTopology, pk=topo_id)
    used: dict = {}  # node_pk -> set of port names
    for lk in topo.links.select_related('node_a', 'node_b').all():
        used.setdefault(lk.node_a_id, set()).add(lk.port_a or '')
        used.setdefault(lk.node_b_id, set()).add(lk.port_b or '')

    result = []
    for n in topo.nodes.select_related('device').all():
        ports = list((n.extra or {}).get('ports') or [])
        for p in ports:
            pname = p.get('name', p) if isinstance(p, dict) else str(p)
            reserved = False
            in_use = pname in (used.get(n.pk) or set())
            result.append({
                'node_id': f'node_{n.pk}',
                'node_label': n.label,
                'port': pname,
                'available': not in_use and not reserved,
                'in_use': in_use,
                'reserved': reserved,
            })
    return JsonResponse({'topology_id': topo_id, 'ports': result})


@login_required
@require_http_methods(['GET'])
def lab_topology_graph_json(request, topo_id):
    """NormalizedGraph v1 — additive; does not replace fabric.json."""
    from .topology_graph import TopologyGraphBuilder

    want_live = request.GET.get('live', '1') != '0'
    want_lldp = request.GET.get('lldp', '1') != '0'
    want_ocs = request.GET.get('ocs', '1') != '0'
    want_planned = request.GET.get('planned', '1') != '0'
    want_res = request.GET.get('reservations', '1') != '0'
    get_object_or_404(LabTopology, pk=topo_id)
    builder = TopologyGraphBuilder(topo_id=topo_id)
    graph = builder.build(
        live=want_live,
        lldp=want_lldp,
        ocs=want_ocs,
        planned=want_planned,
        reservations=want_res,
    )
    return JsonResponse(graph)


@login_required
@require_http_methods(['GET'])
def lab_topology_graph_compare(request, topo_id):
    """Side-by-side legacy fabric.json vs graph builder (validation)."""
    from .topology_graph import compare_fabric_legacy_vs_graph

    get_object_or_404(LabTopology, pk=topo_id)
    want_live = request.GET.get('live', '1') != '0'
    want_lldp = request.GET.get('lldp', '1') != '0'
    return JsonResponse(compare_fabric_legacy_vs_graph(topo_id, live=want_live, lldp=want_lldp))


@login_required
@require_http_methods(['POST'])
def lab_topology_graph_refresh(request, topo_id):
    """Invalidate graph cache; optional background LLDP refresh."""
    from .topology_graph import invalidate_cache

    get_object_or_404(LabTopology, pk=topo_id)
    try:
        body = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        body = {}
    sources = body.get('sources') or []
    invalidate_cache(topo_id)
    try:
        from .topology_fabric_cache import (
            invalidate_topology_fabric_cache,
            schedule_port_fabric_refresh,
        )
        invalidate_topology_fabric_cache(topo_id)
        schedule_port_fabric_refresh(topo_id, want_live=True, chassis_ssh=True)
    except Exception as exc:
        logger.warning('fabric snapshot invalidate/refresh: %s', exc)
    if 'lldp' in sources:
        _schedule_switch_lldp_refresh(topo_id)
    return JsonResponse({'ok': True, 'started': True, 'invalidated': True})


@login_required
@require_http_methods(['POST'])
def lab_topology_graph_validate(request, topo_id):
    """Pre-save / import validation against live graph conflicts."""
    from .topology_graph import validate_topology_payload

    get_object_or_404(LabTopology, pk=topo_id)
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid JSON')
    result = validate_topology_payload(topo_id, payload)
    return JsonResponse(result)


@login_required
@require_http_methods(['GET'])
def lab_fabric_map_api(request, topo_id):
    """
    Live fabric map for the draw.io-style visualization.

    Combines:
      1. Topology nodes/links (planned physical cables from DB)
      2. Live OCS crossconnects (fetched from Calient)
      3. LLDP adjacencies from Device cache

    Query params:
      live=1   (default)  — fetch live OCS data; 0 = use cached only
      lldp=1              — include LLDP adjacency edges

    Response schema:
      {
        "nodes": [{id, label, ip, node_type, vendor, x, y, ports, status},...],
        "links": [{id, type, source, target, port_a, port_b,
                   health, color, label, ocs_triplets, loss_db},...],
        "ocs_summary": {total, active, alarm, unmapped},
        "fetched_at": ISO
      }
    """
    topo = get_object_or_404(LabTopology, pk=topo_id)
    want_live = request.GET.get('live', '1') != '0'
    want_lldp = request.GET.get('lldp', '1') != '0'

    if TOPOLOGY_GRAPH_FABRIC:
        from .topology_graph import TopologyGraphBuilder
        builder = TopologyGraphBuilder(topo_id=topo_id)
        graph = builder.build(live=want_live, lldp=want_lldp, ocs=want_live, planned=True)
        payload = builder.to_fabric_legacy(graph)
        payload['topo_id'] = topo_id
        payload['topo_name'] = topo.name
        payload['_graph_builder'] = True
        return JsonResponse(payload)

    from datetime import datetime, timezone as _tz
    import traceback

    # ── 1. Base nodes from topology DB ────────────────────────────────────────
    # Build chassis label→IP from site JSON (most reliable source for OCS lab)
    import json as _json
    from pathlib import Path as _Path
    _SITE_FILES = sorted((_Path(__file__).parent.parent / 'resources').glob('ocs_photonic_site*.json'))
    _chassis_name_to_ip = {}  # normalized label → ip
    for _sf in _SITE_FILES:
        try:
            _sd = _json.loads(_sf.read_text())
            for _kc in (_sd.get('keysight_chassis') or []):
                _nm = (_kc.get('name') or '').lower().replace('_', '').replace('-', '').replace(' ', '')
                if _nm and _kc.get('ip'):
                    _chassis_name_to_ip[_nm] = _kc['ip']
        except Exception:
            pass
    # Also from DB KeysightChassis
    from .models import KeysightChassis as _KCh
    for _kc in _KCh.objects.all():
        for _key in [_kc.hostname, _kc.ip_address]:
            if _key:
                _chassis_name_to_ip[_key.lower().replace('_','').replace('-','').replace(' ','')] = _kc.ip_address

    def _resolve_chassis_ip(label):
        lbl = label.lower().replace('-', '').replace(' ', '').replace('_', '')
        # Exact
        if lbl in _chassis_name_to_ip:
            return _chassis_name_to_ip[lbl]
        # Digit suffix match: "aresoneM05" → "aresone5"
        import re as _re
        m = _re.search(r'(\d+)$', lbl)
        if m:
            num = str(int(m.group(1)))
            base = lbl[:lbl.rfind(m.group(1))]
            for k, v in _chassis_name_to_ip.items():
                kb = k.rstrip('0123456789')
                kn = k[len(kb):]
                if kn == num and base[:4] == kb[:4]:
                    return v
        return ''

    node_rows = {}
    for n in topo.nodes.select_related('device').all():
        ip = n.device.ip_address if n.device else (n.extra or {}).get('device_ip', '')
        if not ip and n.node_type == 'chassis':
            ip = _resolve_chassis_ip(n.label)
        vendor = (n.device.vendor_type if n.device else '') or n.node_type
        node_rows[n.pk] = {
            'id': f'node_{n.pk}',
            'db_pk': n.pk,
            'label': n.label,
            'ip': ip,
            'node_type': n.node_type,
            'vendor': vendor,
            'x': n.x,
            'y': n.y,
            'ports': list((n.extra or {}).get('ports') or []),
            'status': 'unknown',
            'device_id': n.device_id,
        }

    # ── 2. Planned physical links ──────────────────────────────────────────────
    links = []
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
            'cable_type': lk.cable_type or 'dac',
            'health': 'planned',
            'color': '#64748b',
            'label': lk.label or lk.cable_type or '',
            'ocs_triplets': [],
            'loss_db': None,
        })

    # ── 3. Live OCS crossconnects ─────────────────────────────────────────────
    ocs_summary = {'total': 0, 'active': 0, 'alarm': 0, 'unmapped': 0, 'error': ''}
    if want_live:
        ocs_node = topo.nodes.filter(node_type='ocs').select_related('device').first()
        if ocs_node and ocs_node.device:
            try:
                from .drivers import get_driver
                from . import ocs_helpers

                ocs_dev = ocs_node.device
                drv = get_driver(ocs_dev)
                rows = drv.fetch_crossconnect_list()
                xcons = (drv.get_ocs_crossconnects(raw_rows=rows).data or [])

                all_dev = list(__import__('connect.models', fromlist=['Device']).Device.objects.all()
                               if False else [])
                # Import models cleanly
                from .models import Device as _Dev
                triplet_map = ocs_helpers.build_ocs_triplet_map(
                    ocs_dev.ip_address, _Dev.objects.all()
                )

                # Build IP→node id AND device_id→node id reverse maps
                ip_to_node = {v['ip']: v['id'] for v in node_rows.values() if v['ip']}
                devid_to_node = {v['device_id']: v['id'] for v in node_rows.values() if v['device_id']}

                def resolve_end(endpoint_info):
                    if not endpoint_info:
                        return None
                    # Try device_id first (works for chassis without IP in topology)
                    did = endpoint_info.get('device_id')
                    if did and did in devid_to_node:
                        return devid_to_node[did]
                    # Fall back to IP
                    eip = endpoint_info.get('ip', '')
                    return ip_to_node.get(eip)

                ocs_summary['total'] = len(xcons)
                _ALARM_CODES = {'CR', 'MJ', 'MN'}
                for xc in xcons:
                    ta = ocs_helpers.norm_ocs_triplet_key(xc.get('port_a', ''))
                    tb = ocs_helpers.norm_ocs_triplet_key(xc.get('port_b', ''))
                    ea = triplet_map.get(ta)
                    eb = triplet_map.get(tb)

                    h1 = xc.get('h1', {})
                    h2 = xc.get('h2', {})
                    # Calient alarm codes: CL=clear/ok, CR=critical, MJ=major, MN=minor
                    # Use h2 (forward measurement) as primary health signal
                    alarm = (h2.get('alarm', 'CL') in _ALARM_CODES) or (h2.get('oc', 'OK') not in ('OK', ''))
                    loss = h2.get('loss')

                    if alarm:
                        health = 'alarm'
                        color = '#ef4444'
                        ocs_summary['alarm'] += 1
                    else:
                        health = 'active'
                        color = '#22c55e'
                        ocs_summary['active'] += 1

                    src_id = resolve_end(ea)
                    dst_id = resolve_end(eb)

                    if not src_id or not dst_id:
                        ocs_summary['unmapped'] += 1
                        # Still emit edge anchored to OCS node if possible
                        ocs_node_id = f'node_{ocs_node.pk}'
                        src_id = src_id or ocs_node_id
                        dst_id = dst_id or ocs_node_id

                    links.append({
                        'id': f'ocs_{xc.get("name", ta + "_" + tb)}',
                        'type': 'ocs_active',
                        'source': src_id,
                        'target': dst_id,
                        'port_a': (ea or {}).get('sw_port', ''),
                        'port_b': (eb or {}).get('sw_port', ''),
                        'cable_type': 'optic',
                        'health': health,
                        'color': color,
                        'label': xc.get('name', ''),
                        'ocs_triplets': [ta, tb],
                        'loss_db': loss,
                        'ocs_path_id': xc.get('name', ''),
                    })
            except Exception as exc:
                logger.warning('fabric_map OCS fetch failed: %s', exc)
                ocs_summary['error'] = str(exc)

    # ── 4. LLDP adjacency edges (from TopologyLink / ChassisDeviceLink cache) ─
    if want_lldp:
        try:
            from .topology import get_cached_topology, CHASSIS_NODE_PREFIX

            lldp_topo = get_cached_topology() or {}
            ip_to_node = {v['ip']: v['id'] for v in node_rows.values() if v['ip']}
            devid_to_node = {v['device_id']: v['id'] for v in node_rows.values() if v.get('device_id')}
            chassisid_to_node = {}
            for n in topo.nodes.all():
                cid = (n.extra or {}).get('chassis_id')
                if cid:
                    chassisid_to_node[int(cid)] = f'node_{n.pk}'
                if n.node_type == 'chassis' and n.label:
                    ip = (n.extra or {}).get('device_ip') or _resolve_chassis_ip(n.label)
                    if ip:
                        ip_to_node[ip] = f'node_{n.pk}'

            def _map_global_node(gid: str):
                if gid.startswith(CHASSIS_NODE_PREFIX):
                    try:
                        ch_id = int(gid[len(CHASSIS_NODE_PREFIX):])
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

            existing_pairs = {
                tuple(sorted((lk['source'], lk['target'])))
                for lk in links if lk.get('source') and lk.get('target')
            }
            for e in lldp_topo.get('links') or []:
                sid = _map_global_node(str(e.get('source', '')))
                tid = _map_global_node(str(e.get('target', '')))
                if not sid or not tid or sid == tid:
                    continue
                pair = tuple(sorted((sid, tid)))
                if pair in existing_pairs:
                    continue
                existing_pairs.add(pair)
                links.append({
                    'id': f'lldp_{sid}_{tid}_{len(links)}',
                    'type': 'lldp',
                    'source': sid,
                    'target': tid,
                    'port_a': e.get('local_port', ''),
                    'port_b': e.get('remote_port', ''),
                    'cable_type': 'direct',
                    'health': 'lldp_only',
                    'color': '#f59e0b',
                    'label': e.get('label') or 'LLDP',
                    'ocs_triplets': [],
                    'loss_db': None,
                })
        except Exception as exc:
            logger.debug('fabric_map LLDP fetch skipped: %s', exc)

    topo_extra = dict(topo.extra or {})
    return JsonResponse({
        'nodes': list(node_rows.values()),
        'links': links,
        'ocs_summary': ocs_summary,
        'fetched_at': datetime.now(_tz.utc).isoformat(),
        'topo_id': topo_id,
        'topo_name': topo.name,
        'view_layouts': topo_extra.get('view_layouts') or {},
    })


@login_required
@require_http_methods(['GET'])
def lab_fabric_map_page(request, topo_id):
    """Interactive draw.io-style fabric map page."""
    from connect.lab_topology_split import get_sub_topology_nav

    topo = get_object_or_404(LabTopology, pk=topo_id)
    return render(request, 'connect/lab_fabric_map.html', {
        'topo': topo,
        'sub_nav': get_sub_topology_nav(topo),
    })


@login_required
@require_http_methods(['GET', 'POST'])
def lab_topology_discover_dac(request, topo_id):
    """
    Find DAC / back-to-back links by matching transceiver serial numbers on ports.

    GET — preview pairs (no DB writes).
    POST — apply discovered links as LabTopologyLink rows.
      Body: { "apply": true, "refresh_cache": false }
    """
    topo = get_object_or_404(LabTopology, pk=topo_id)
    from .topology_dac_finder import (
        apply_serial_links_to_topology,
        discover_serial_links,
    )

    refresh = request.GET.get('refresh') in ('1', 'true', 'yes')
    body: Dict[str, Any] = {}
    if request.method == 'POST':
        try:
            body = json.loads(request.body or '{}')
        except (json.JSONDecodeError, ValueError):
            body = {}
        refresh = refresh or body.get('refresh_cache') in (True, '1', 'true', 'yes')

    result = discover_serial_links(topo, refresh_cache=refresh)
    do_apply = request.method == 'POST' and body.get('apply') in (True, '1', 'true', 'yes')
    if not do_apply:
        return JsonResponse({'ok': True, 'applied': False, **result})

    stats = apply_serial_links_to_topology(topo, result['pairs'])
    log_topology_action(
        request.user, 'dac_serial_discover', topology=topo,
        detail=f"created {stats['created']} serial links", request=request,
    )
    return JsonResponse({'ok': True, 'applied': True, **result, **stats})


@login_required
@require_http_methods(['POST'])
def lab_topology_build_dc(request):
    """
    One-click DC lab topology from resources/lab_topology_schema.json + site JSON.

    POST JSON:
      name — topology title
      preset — 'hbg' | 'v6' (example schemas; IPv4 uses lab_topology_schema.json)
      auto_serial — run serial DAC/B2B discovery (default true)
      auto_lldp — add LLDP links from cached topology (default true)
      refresh_cache — refresh IxOS/Keysight chassis port cache before wiring (default false)
      device_ids — extra Device PKs to add (e.g. FortiGate DUT)
    """
    from pathlib import Path
    from .topology_dac_finder import (
        apply_lldp_links_to_topology,
        apply_serial_links_to_topology,
        discover_serial_links,
        enrich_topology_ports_from_cache,
    )

    try:
        body = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')

    from connect.lab_topology_io import resource_paths_for_profile

    resources = Path(__file__).resolve().parent.parent / 'resources'
    preset = (body.get('preset') or 'hbg').strip().lower()
    layout_name, site_name = resource_paths_for_profile(preset)
    layout_path = resources / layout_name
    site_path = resources / site_name
    if not layout_path.is_file():
        return JsonResponse({'ok': False, 'error': f'{layout_name} not found'}, status=404)

    try:
        layout_data = json.loads(layout_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        return JsonResponse({'ok': False, 'error': f'Invalid layout JSON: {exc}'}, status=400)

    site_data: Dict = {}
    site_file = layout_data.get('site_json') or site_name
    site_candidate = resources / site_file
    if site_candidate.is_file():
        try:
            site_data = json.loads(site_candidate.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass
    elif site_path.is_file():
        try:
            site_data = json.loads(site_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass

    name = (body.get('name') or '').strip() or layout_data.get('label') or 'DC Lab Topology'
    if preset in ('v6', 'hbg-v6', 'ipv6', 'dc-v6'):
        name = (body.get('name') or '').strip() or 'Example DC topology (IPv6 mgmt)'
    elif not (body.get('name') or '').strip():
        name = layout_data.get('label') or 'Example DC topology'

    tag_parts = ['dc', preset] if preset else ['dc']
    if preset in ('v6', 'hbg-v6', 'ipv6', 'dc-v6'):
        tag_parts.extend(['v6', 'ocs', 'arista', 'sonic'])

    topo = LabTopology.objects.create(
        name=name,
        description=layout_data.get('description') or f'Built from {layout_path.name} ({preset})',
        source='dc_preset',
        tags=','.join(dict.fromkeys(t for t in tag_parts if t)),
        extra={
            'addressing_profile': 'v6' if preset in ('v6', 'hbg-v6', 'ipv6', 'dc-v6') else 'ipv4',
            'site_json': site_file,
            'layout_schema': layout_name,
            'resource_bundle': {
                'addressing_profile': 'v6' if preset in ('v6', 'hbg-v6', 'ipv6', 'dc-v6') else 'ipv4',
                'site_json': site_file,
                'layout_schema': layout_name,
            },
        },
        created_by=request.user,
    )

    try:
        msg = _import_topology_layout(topo, layout_data, site_data)
    except Exception as exc:
        logger.exception('lab_topology_build_dc import failed')
        topo.delete()
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)

    refresh_cache = body.get('refresh_cache') in (True, '1', 'true', 'yes')
    ports_updated = enrich_topology_ports_from_cache(topo, site_data, refresh=refresh_cache)

    extra_ids = body.get('device_ids') or []
    x_off = 1400
    for jdx, did in enumerate(extra_ids):
        device = Device.objects.filter(pk=did).first()
        if not device:
            continue
        node_type = _NODE_TYPE_MAP.get(device.vendor_type, 'switch')
        LabTopologyNode.objects.create(
            topology=topo,
            device=device,
            node_key=f'dut_{device.pk}'[:64],
            node_type=node_type,
            label=device.hostname or device.ip_address,
            x=x_off + jdx * 220,
            y=400,
            extra={'ports': _default_ports_for_node(node_type), 'role': 'dut'},
        )

    serial_stats = {'created': 0, 'skipped': 0, 'pairs': 0}
    lldp_created = 0
    if body.get('auto_serial', True) not in (False, '0', 0):
        disc = discover_serial_links(topo, refresh_cache=refresh_cache)
        serial_stats = apply_serial_links_to_topology(topo, disc['pairs'])
        serial_stats['pairs'] = len(disc['pairs'])
    if body.get('auto_lldp', True) not in (False, '0', 0):
        lldp_created = apply_lldp_links_to_topology(topo)

    log_topology_action(
        request.user, 'dc_preset_build', topology=topo,
        detail=f'{msg}; ports={ports_updated}; serial={serial_stats.get("created", 0)}; lldp={lldp_created}',
        request=request,
    )

    return JsonResponse({
        'ok': True,
        'topology_id': topo.pk,
        'name': topo.name,
        'import_msg': msg,
        'ports_enriched': ports_updated,
        'serial_links': serial_stats,
        'lldp_links': lldp_created,
        'redirect': f'/lab-topology/{topo.pk}/',
    }, status=201)


@login_required
@require_http_methods(['GET'])
def lab_topology_node_ports(request, topo_id, node_id):
    """Slot/port grid for chassis, OCS, switch, server, and firewall nodes."""
    from .topology_device_ports import (
        build_node_slot_layout,
        fetch_triplet_to_xcon_for_topo,
        load_site_port_data,
        persist_layout_on_node,
    )

    topo = get_object_or_404(LabTopology, pk=topo_id)
    node = get_object_or_404(LabTopologyNode, pk=node_id, topology=topo)
    refresh = request.GET.get('refresh') in ('1', 'true', 'yes')
    warning = ''

    site_data = load_site_port_data()
    triplet_to_xcon = fetch_triplet_to_xcon_for_topo(topo) if refresh or node.node_type == 'ocs' else {}

    layout = build_node_slot_layout(
        node, topo,
        site_port_data=site_data,
        triplet_to_xcon=triplet_to_xcon,
        refresh=refresh,
    )

    if not layout.get('slots'):
        warning = warning or 'No port layout — bind device/IP or Refresh Ports.'

    if layout.get('slots') and layout.get('source') in (
        'live_cache', 'ocs_live', 'switch_site', 'stored_chassis',
    ):
        persist_layout_on_node(node, layout)

    ch_id = (node.extra or {}).get('chassis_id')
    return JsonResponse({
        'ok': True,
        'node_id': node.pk,
        'node_type': node.node_type,
        'chassis_id': ch_id,
        'warning': warning,
        **layout,
    })


@login_required
@require_http_methods(['POST'])
def lab_topology_validate_link(request, topo_id):
    """
    Validate a planned link by flapping one switch port and observing the peer.

    POST JSON: { "link_id": int } or { "node_a", "port_a", "node_b", "port_b" }
    """
    from .topology_link_validate import validate_link_by_flap

    topo = get_object_or_404(LabTopology, pk=topo_id)
    try:
        body = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')

    link = None
    link_id = body.get('link_id')
    if link_id:
        link = topo.links.filter(pk=int(link_id)).first()
    if not link:
        na = body.get('node_a')
        nb = body.get('node_b')
        if na and nb:
            link = topo.links.filter(
                node_a_id=na, node_b_id=nb,
            ).first() or topo.links.filter(node_a_id=nb, node_b_id=na).first()
    if not link:
        return JsonResponse({'ok': False, 'error': 'Link not found'}, status=404)

    refresh = body.get('refresh_peer', True) not in (False, '0', 0)
    try:
        result = validate_link_by_flap(link, refresh_peer=refresh)
    except Exception as exc:
        logger.exception('validate_link topo=%s link=%s', topo_id, link.pk)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)

    log_topology_action(
        request.user, 'link_validated', topology=topo,
        detail=f'link {link.pk} validated={result.get("validated")}', request=request,
    )
    return JsonResponse(result)


@login_required
@require_http_methods(['POST', 'DELETE'])
def lab_topology_delete(request, topo_id):
    """Delete a lab topology and all its nodes/links (CASCADE).

    Accepts POST (from HTML form) or DELETE (from JS fetch).
    Returns JSON on AJAX (X-Requested-With: XMLHttpRequest) or redirects to list.
    """
    topo = get_object_or_404(LabTopology, pk=topo_id)
    name = topo.name
    topo.delete()
    logger.info("Lab topology #%d '%s' deleted by %s", topo_id, name, request.user)

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' \
              or request.content_type == 'application/json' \
              or request.method == 'DELETE'
    if is_ajax:
        return JsonResponse({'ok': True, 'deleted': name})
    return redirect('lab_topology_list')
