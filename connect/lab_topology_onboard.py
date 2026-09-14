"""Unified topology onboarding wizard (Site JSON, DC preset, inventory, Site v1)."""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods

from connect.lab_site_compiler import compile_site_v1, merge_site_mapping_from_file
from connect.models import Device, KeysightChassis, LabTopology

logger = logging.getLogger(__name__)


def _preview_counts(topo: LabTopology | None = None, *, nodes: int = 0, links: int = 0) -> dict[str, int]:
    if topo:
        return {'nodes': topo.nodes.count(), 'links': topo.links.count()}
    return {'nodes': nodes, 'links': links}


@login_required
@require_GET
def lab_topology_onboard(request):
    """Multi-step topology onboarding wizard."""
    resources = Path(__file__).resolve().parent.parent / 'resources'
    presets = [
        {'id': 'hbg', 'label': 'Example DC (IPv4)'},
        {'id': 'v6', 'label': 'Example DC (IPv6 mgmt)'},
        {'id': 'ocs_only', 'label': 'OCS photonic only (site JSON)'},
        {'id': 'inventory', 'label': 'Inventory + LLDP enrich'},
        {'id': 'site_v1', 'label': 'LabVault Site v1 (minimal YAML/JSON)'},
    ]
    return render(request, 'connect/lab_topology_onboard.html', {
        'presets': presets,
        'devices': Device.objects.order_by('hostname', 'ip_address'),
        'chassis': KeysightChassis.objects.order_by('hostname', 'ip_address'),
        'wizard_enabled': True,
        'mapping_site': 'ocs_photonic_site.json',
        'resources_exist': (resources / 'ocs_photonic_site.json').is_file(),
    })


@login_required
@require_http_methods(['POST'])
def lab_topology_onboard_preview(request):
    """Validate inputs and return preview without creating topology."""
    try:
        body = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')

    mode = (body.get('mode') or '').strip().lower()
    out: dict[str, Any] = {'ok': True, 'mode': mode, 'warnings': []}

    if mode == 'site_v1':
        content = body.get('site_v1_text', '')
        if not content:
            return JsonResponse({'ok': False, 'error': 'site_v1_text required'}, status=400)
        try:
            doc = json.loads(content) if content.lstrip().startswith('{') else __import__('yaml').safe_load(content)
            site = compile_site_v1(doc)
            resources = Path(__file__).resolve().parent.parent / 'resources'
            mapping = body.get('mapping_site') or 'ocs_photonic_site.json'
            site = merge_site_mapping_from_file(site, resources / mapping)
            out['site_keys'] = list(site.keys())
            out['switch_count'] = len(site.get('arista_switches') or [])
            out['chassis_count'] = len(site.get('ares_switches') or [])
            out['has_ocs'] = bool(site.get('ocs_controller'))
        except Exception as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    elif mode in ('hbg', 'v6', 'dc_preset'):
        from connect.lab_topology_io import resource_paths_for_profile
        layout_name, site_name = resource_paths_for_profile(mode if mode != 'dc_preset' else body.get('preset', 'hbg'))
        resources = Path(__file__).resolve().parent.parent / 'resources'
        out['layout'] = layout_name
        out['site'] = site_name
        out['layout_exists'] = (resources / layout_name).is_file()
        out['site_exists'] = (resources / site_name).is_file()
    elif mode == 'ocs_only':
        if not body.get('site_json_text') and not body.get('site_path'):
            out['warnings'].append('Provide site JSON upload text')
    elif mode == 'inventory':
        out['device_ids'] = body.get('device_ids') or []
        out['chassis_ids'] = body.get('chassis_ids') or []
    else:
        return JsonResponse({'ok': False, 'error': f'Unknown mode: {mode}'}, status=400)

    return JsonResponse(out)


@login_required
@require_http_methods(['POST'])
def lab_topology_onboard_build(request):
    """Execute build after preview."""
    from connect import lab_topology_views as ltv
    from connect.lab_topology_ocs_json import build_lab_topology_from_ocs_json
    from connect.ocs_site_config_io import run_ocs_site_import

    try:
        body = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')

    mode = (body.get('mode') or '').strip().lower()
    name = (body.get('name') or '').strip() or 'Onboarded Lab Topology'
    resources = Path(__file__).resolve().parent.parent / 'resources'

    if mode in ('hbg', 'v6'):
        from django.test import RequestFactory
        rf = RequestFactory()
        req = rf.post(
            '/lab-topology/build-dc/',
            data=json.dumps({
                'name': name,
                'preset': mode,
                'auto_serial': body.get('auto_serial', True),
                'auto_lldp': body.get('auto_lldp', True),
                'refresh_cache': body.get('refresh_cache', False),
                'device_ids': body.get('device_ids') or [],
            }),
            content_type='application/json',
        )
        req.user = request.user
        return ltv.lab_topology_build_dc(req)

    if mode == 'ocs_only':
        site_text = body.get('site_json_text', '')
        if not site_text:
            return JsonResponse({'ok': False, 'error': 'site_json_text required'}, status=400)
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as tmp:
            tmp.write(site_text)
            tmp_path = tmp.name
        try:
            site_data = json.loads(site_text)
            run_ocs_site_import(site_data)
            topo = build_lab_topology_from_ocs_json(tmp_path, name, request.user, description='Onboard wizard OCS')
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return JsonResponse({
            'ok': True, 'topology_id': topo.pk, 'redirect': f'/lab-topology/{topo.pk}/',
            'counts': _preview_counts(topo),
        }, status=201)

    if mode == 'site_v1':
        content = body.get('site_v1_text', '')
        if not content:
            return JsonResponse({'ok': False, 'error': 'site_v1_text required'}, status=400)
        try:
            if content.lstrip().startswith('{'):
                doc = json.loads(content)
            else:
                import yaml
                doc = yaml.safe_load(content)
            site = compile_site_v1(doc)
            mapping = body.get('mapping_site') or 'ocs_photonic_site.json'
            site = merge_site_mapping_from_file(site, resources / mapping)
            run_ocs_site_import(site)
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as tmp:
                json.dump(site, tmp)
                tmp_path = tmp.name
            try:
                topo = build_lab_topology_from_ocs_json(tmp_path, name, request.user, description='Site v1 compiler')
            finally:
                Path(tmp_path).unlink(missing_ok=True)
            preset = (doc.get('preset') or '').strip().lower()
            if preset in ('hbg', 'v6', 'hbg_v6'):
                from django.test import RequestFactory
                rf = RequestFactory()
                preset_key = 'v6' if preset in ('v6', 'hbg_v6') else preset
                req = rf.post(
                    '/lab-topology/build-dc/',
                    data=json.dumps({'name': name + ' (DC layout)', 'preset': preset_key, 'auto_serial': True, 'auto_lldp': True}),
                    content_type='application/json',
                )
                req.user = request.user
                return ltv.lab_topology_build_dc(req)
            return JsonResponse({
                'ok': True, 'topology_id': topo.pk, 'redirect': f'/lab-topology/{topo.pk}/',
                'counts': _preview_counts(topo),
            }, status=201)
        except Exception as exc:
            logger.exception('site_v1 build failed')
            return JsonResponse({'ok': False, 'error': str(exc)}, status=500)

    if mode == 'inventory':
        from django.test import RequestFactory
        rf = RequestFactory()
        req = rf.post(
            '/lab-topology/build-inventory/',
            data=json.dumps({
                'name': name,
                'device_ids': body.get('device_ids') or [],
                'chassis_ids': body.get('chassis_ids') or [],
                'auto_lldp': body.get('auto_lldp', True),
                'auto_serial': body.get('auto_serial', True),
            }),
            content_type='application/json',
        )
        req.user = request.user
        return ltv.lab_topology_build_inventory(req)

    return JsonResponse({'ok': False, 'error': f'Unknown mode: {mode}'}, status=400)
