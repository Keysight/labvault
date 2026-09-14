"""
Multibundle export/import: inventory JSON, topology v3, site schemas, optional DBs and cache.

Tarball layout::

    manifest.json
    inventory.json              # labvault-full-export (scoped or full)
    topologies/<slug>.json      # labvault.lab_topology v3
    resources/<file>.json       # site + layout schemas
    data/lldp_persistent_cache.json
    databases/db.sqlite3          # optional
    databases/np_timeseries.sqlite3
    secrets/<name>              # optional, off by default
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils import timezone

from .lab_topology_io import export_topology, import_topology
from .labvault_dataset import (
    FORMAT_ID as INVENTORY_FORMAT_ID,
    build_export_payload,
    import_from_payload,
)
from .models import Device, KeysightChassis, LabTopology

BUNDLE_FORMAT = 'labvault-multibundle'
BUNDLE_VERSION = 1


def _slug(name: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9]+', '-', (name or 'topology').strip()).strip('-').lower()
    return s or 'topology'


def _device_ips_for_scope(*, lab: str = '', ip_prefix: str = '') -> set[str]:
    ips: set[str] = set()
    if lab:
        ips.update(
            KeysightChassis.objects.filter(lab_name=lab).values_list('ip_address', flat=True)
        )
    dev_qs = Device.objects.all()
    if lab:
        dev_qs = dev_qs.filter(tags__icontains='ocs-lab') | dev_qs.filter(
            tags__icontains=lab.lower().replace(' ', '-')
        )
    if ip_prefix:
        dev_qs = dev_qs.filter(ip_address__startswith=ip_prefix)
    ips.update(dev_qs.values_list('ip_address', flat=True))
    if ip_prefix:
        ips.update(
            KeysightChassis.objects.filter(ip_address__startswith=ip_prefix).values_list(
                'ip_address', flat=True
            )
        )
    return {ip.strip() for ip in ips if (ip or '').strip()}


def build_scoped_inventory_payload(
    *,
    lab: str = '',
    ip_prefix: str = '',
    include_capex: bool = False,
) -> dict:
    """Subset of :func:`build_export_payload` for one lab / IP prefix."""
    payload = build_export_payload()
    if not lab and not ip_prefix:
        if not include_capex:
            payload.pop('capex', None)
        return payload

    ips = _device_ips_for_scope(lab=lab, ip_prefix=ip_prefix)
    payload['devices'] = [
        d for d in payload.get('devices', [])
        if (d.get('ip_address') or '').strip() in ips
    ]
    if lab:
        payload['keysight_chassis'] = [
            c for c in payload.get('keysight_chassis', [])
            if (c.get('lab_name') or '').strip() == lab
            or (c.get('ip_address') or '').strip() in ips
        ]
    elif ip_prefix:
        payload['keysight_chassis'] = [
            c for c in payload.get('keysight_chassis', [])
            if (c.get('ip_address') or '').strip().startswith(ip_prefix)
        ]

    chassis_ips = {(c.get('ip_address') or '').strip() for c in payload['keysight_chassis']}
    payload['keysight_reservation_items'] = [
        x for x in payload.get('keysight_reservation_items', [])
        if (x.get('chassis_ip') or '').strip() in chassis_ips
    ]
    payload['topology_links'] = [
        lnk for lnk in payload.get('topology_links', [])
        if (lnk.get('device_a_ip') or '').strip() in ips
        and (lnk.get('device_b_ip') or '').strip() in ips
    ]
    payload['config_backups'] = [
        b for b in payload.get('config_backups', [])
        if (b.get('device_ip') or '').strip() in ips
    ]
    if not include_capex:
        payload.pop('capex', None)

    payload['scope'] = {'lab': lab or None, 'ip_prefix': ip_prefix or None, 'ips': sorted(ips)}
    payload['counts'] = {
        'devices': len(payload['devices']),
        'keysight_chassis': len(payload['keysight_chassis']),
        'lab_topologies': len(payload.get('lab_topologies', [])),
    }
    return payload


def write_bundle(
    output_path: str | Path,
    *,
    lab: str = '',
    ip_prefix: str = '',
    topology_ids: list[int] | None = None,
    topology_names: list[str] | None = None,
    resource_files: list[str] | None = None,
    include_databases: bool = False,
    include_lldp_cache: bool = True,
    include_secrets: bool = False,
    include_capex: bool = False,
) -> dict:
    """Write a ``.tar.gz`` multibundle and return manifest summary."""
    output_path = Path(output_path)
    base = Path(settings.BASE_DIR)
    resource_files = list(resource_files or [])
    if not resource_files:
        resource_files = [
            'resources/ocs_photonic_site.json',
            'resources/lab_topology_schema.json',
        ]

    inventory = build_scoped_inventory_payload(
        lab=lab, ip_prefix=ip_prefix, include_capex=include_capex,
    )

    topo_qs = LabTopology.objects.all()
    if topology_ids:
        topo_qs = topo_qs.filter(pk__in=topology_ids)
    if topology_names:
        from django.db.models import Q
        q = Q()
        for name in topology_names:
            q |= Q(name__icontains=name)
        topo_qs = topo_qs.filter(q)
    if lab or ip_prefix:
        # Drop unrelated topologies from scoped inventory embed
        allowed_ids = {t.pk for t in topo_qs}
        allowed_names = {x.name for x in topo_qs}
        inventory['lab_topologies'] = [
            t for t in inventory.get('lab_topologies', [])
            if t.get('topology', {}).get('id') in allowed_ids
            or t.get('topology', {}).get('name') in allowed_names
        ]
        if inventory.get('counts'):
            inventory['counts']['lab_topologies'] = len(inventory['lab_topologies'])

    manifest: dict[str, Any] = {
        'format': BUNDLE_FORMAT,
        'version': BUNDLE_VERSION,
        'exported_at': timezone.now().isoformat(),
        'scope': {'lab': lab or None, 'ip_prefix': ip_prefix or None},
        'files': [],
        'counts': {},
    }

    with tempfile.TemporaryDirectory(prefix='lv-bundle-') as tmp:
        root = Path(tmp)
        (root / 'inventory.json').write_text(
            json.dumps(inventory, indent=2), encoding='utf-8',
        )
        manifest['files'].append('inventory.json')
        manifest['counts']['inventory'] = inventory.get('counts', {})

        topo_dir = root / 'topologies'
        topo_dir.mkdir(exist_ok=True)
        for topo in topo_qs.order_by('pk'):
            slug = _slug(topo.name)
            rel = f'topologies/{slug}-{topo.pk}.json'
            (root / rel).write_text(
                json.dumps(export_topology(topo), indent=2), encoding='utf-8',
            )
            manifest['files'].append(rel)
        manifest['counts']['topologies'] = topo_qs.count()

        res_dir = root / 'resources'
        res_dir.mkdir(exist_ok=True)
        for rel_path in resource_files:
            src = base / rel_path
            if not src.is_file():
                continue
            dest = f'resources/{src.name}'
            shutil.copy2(src, root / dest)
            manifest['files'].append(dest)

        if include_lldp_cache:
            cache_src = base / 'data' / 'lldp_persistent_cache.json'
            if cache_src.is_file():
                (root / 'data').mkdir(exist_ok=True)
                shutil.copy2(cache_src, root / 'data' / 'lldp_persistent_cache.json')
                manifest['files'].append('data/lldp_persistent_cache.json')

        if include_databases:
            db_dir = root / 'databases'
            db_dir.mkdir(exist_ok=True)
            for name in ('db.sqlite3', 'np_timeseries.sqlite3'):
                src = base / name
                if src.is_file():
                    shutil.copy2(src, db_dir / name)
                    manifest['files'].append(f'databases/{name}')

        if include_secrets:
            sec_src = base / 'secrets'
            if sec_src.is_dir():
                (root / 'secrets').mkdir(exist_ok=True)
                for f in sec_src.iterdir():
                    if f.is_file() and f.stat().st_size > 0:
                        dest = f'secrets/{f.name}'
                        shutil.copy2(f, root / dest)
                        manifest['files'].append(dest)

        manifest['inventory_format'] = INVENTORY_FORMAT_ID
        (root / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output_path, 'w:gz') as tar:
            tar.add(root / 'manifest.json', arcname='manifest.json')
            for rel in manifest['files']:
                tar.add(root / rel, arcname=rel)

    return manifest


def extract_bundle(bundle_path: str | Path, dest_dir: str | Path) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle_path, 'r:gz') as tar:
        tar.extractall(dest_dir)
    return dest_dir


def import_bundle(
    bundle_path: str | Path,
    *,
    import_capex: bool = False,
    replace_topologies: bool = True,
    import_databases: bool = False,
    import_lldp_cache: bool = True,
    import_secrets: bool = False,
) -> dict:
    """Import a multibundle produced by :func:`write_bundle`."""
    base = Path(settings.BASE_DIR)
    stats: dict[str, Any] = {'topologies': [], 'files': []}

    with tempfile.TemporaryDirectory(prefix='lv-bundle-import-') as tmp:
        root = extract_bundle(bundle_path, tmp)
        manifest_path = root / 'manifest.json'
        if not manifest_path.is_file():
            raise ValueError('Bundle missing manifest.json')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('format') != BUNDLE_FORMAT:
            raise ValueError(
                f"Expected format {BUNDLE_FORMAT!r}, got {manifest.get('format')!r}"
            )
        stats['manifest'] = manifest

        inv_path = root / 'inventory.json'
        if inv_path.is_file():
            payload = json.loads(inv_path.read_text(encoding='utf-8'))
            stats['inventory'] = import_from_payload(
                payload, import_capex=import_capex,
            )

        site_data = None
        site_path = root / 'resources' / 'ocs_photonic_site.json'
        if site_path.is_file():
            site_data = json.loads(site_path.read_text(encoding='utf-8'))
            from .ocs_site_config_io import run_ocs_site_import
            stats['site_import'] = run_ocs_site_import(site_data)

        topo_dir = root / 'topologies'
        if topo_dir.is_dir():
            for topo_file in sorted(topo_dir.glob('*.json')):
                payload = json.loads(topo_file.read_text(encoding='utf-8'))
                name = (payload.get('topology') or {}).get('name') or payload.get('name') or ''
                topo = LabTopology.objects.filter(name=name).first() if name else None
                if topo is None:
                    topo = LabTopology.objects.create(
                        name=name or f'Imported {topo_file.stem}',
                        description=(payload.get('topology') or {}).get('description', ''),
                        source=(payload.get('topology') or {}).get('source', 'bundle-import'),
                        tags=(payload.get('topology') or {}).get('tags', ''),
                    )
                elif replace_topologies:
                    topo.nodes.all().delete()
                    topo.links.all().delete()
                msg = import_topology(topo, payload, site_data=site_data)
                stats['topologies'].append({'name': topo.name, 'id': topo.pk, 'detail': msg})

        if import_lldp_cache:
            cache_src = root / 'data' / 'lldp_persistent_cache.json'
            if cache_src.is_file():
                cache_dest = base / 'data' / 'lldp_persistent_cache.json'
                cache_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cache_src, cache_dest)
                stats['lldp_cache'] = str(cache_dest)

        if import_databases:
            db_src = root / 'databases'
            if db_src.is_dir():
                for name in ('db.sqlite3', 'np_timeseries.sqlite3'):
                    src = db_src / name
                    if src.is_file():
                        shutil.copy2(src, base / name)
                        stats.setdefault('databases', []).append(name)

        if import_secrets:
            sec_src = root / 'secrets'
            if sec_src.is_dir():
                sec_dest = base / 'secrets'
                sec_dest.mkdir(parents=True, exist_ok=True)
                for f in sec_src.iterdir():
                    if f.is_file():
                        shutil.copy2(f, sec_dest / f.name)
                        stats.setdefault('secrets', []).append(f.name)

    return stats
