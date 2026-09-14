"""Compile LabVault Site v1 YAML/JSON into site JSON + topology import payload."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


def _load_document(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    if p.suffix.lower() in ('.yaml', '.yml'):
        if yaml is None:
            raise RuntimeError('PyYAML required for .yaml site files')
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('Site document must be a mapping/object')
    return data


def compile_site_v1(doc: dict[str, Any]) -> dict[str, Any]:
    """Transform minimal Site v1 document into ocs_photonic_site-style JSON."""
    version = doc.get('version', 1)
    if version != 1:
        raise ValueError(f'Unsupported site version: {version}')

    site: dict[str, Any] = {
        'version': 3,
        'common_tags': list(doc.get('site_tags') or doc.get('common_tags') or []),
    }
    ocs = doc.get('ocs') or {}
    if ocs:
        site['ocs_controller'] = {
            'ip': ocs.get('ip', ''),
            'hostname': ocs.get('hostname', ''),
            'username': ocs.get('user') or ocs.get('username', 'user'),
            'password': ocs.get('password', ''),
            'tags': list(ocs.get('tags') or []),
        }

    arista_switches = []
    for sw in doc.get('switches') or doc.get('arista_switches') or []:
        if not isinstance(sw, dict):
            continue
        row = {
            'ip': sw.get('ip', ''),
            'hostname': sw.get('hostname', ''),
            'username': sw.get('user') or sw.get('username', 'admin'),
            'password': sw.get('password', ''),
            'vendor_type': sw.get('role') or sw.get('vendor_type', 'arista'),
            'tags': list(sw.get('tags') or []),
        }
        if sw.get('mapping') == 'site' or sw.get('use_site_mapping'):
            row['fixed_mapping'] = {'source': 'site_v1'}
        arista_switches.append(row)
    if arista_switches:
        site['arista_switches'] = arista_switches

    ares = []
    for ch in doc.get('chassis') or doc.get('ares_switches') or []:
        if not isinstance(ch, dict):
            continue
        ares.append({
            'ip': ch.get('ip', ''),
            'hostname': ch.get('hostname', ''),
            'username': ch.get('user') or ch.get('username', 'admin'),
            'password': ch.get('password', 'admin'),
            'chassis_type': ch.get('type') or ch.get('chassis_type', 'aresone'),
            'tags': list(ch.get('tags') or []),
        })
    if ares:
        site['ares_switches'] = ares

    return site


def compile_site_v1_file(path: str | Path) -> dict[str, Any]:
    return compile_site_v1(_load_document(path))


def merge_site_mapping_from_file(site_doc: dict[str, Any], mapping_path: str | Path) -> dict[str, Any]:
    """Overlay port_to_ocs_triplets from a full site JSON when Site v1 references it."""
    mp = Path(mapping_path)
    if not mp.is_file():
        return site_doc
    full = json.loads(mp.read_text(encoding='utf-8'))
    by_ip = {}
    for sw in full.get('arista_switches') or []:
        ip = sw.get('ip')
        if ip:
            by_ip[ip] = sw
    for sw in site_doc.get('arista_switches') or []:
        ip = sw.get('ip')
        src = by_ip.get(ip) or {}
        fixed = src.get('fixed_mapping') or {}
        triplets = fixed.get('port_to_ocs_triplets') or src.get('port_to_ocs_triplets')
        if triplets:
            sw['fixed_mapping'] = {'port_to_ocs_triplets': triplets}
    ocs = full.get('ocs_controller')
    if ocs and site_doc.get('ocs_controller'):
        for key in ('password', 'username', 'hostname'):
            if not site_doc['ocs_controller'].get(key) and ocs.get(key):
                site_doc['ocs_controller'][key] = ocs[key]
    return site_doc
