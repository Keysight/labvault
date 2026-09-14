"""
Management command: import_lab_topology

Imports a lab topology from a layout JSON file (and optionally an OCS site JSON)
into a new LabTopology record in the database.

Usage:
  python manage.py import_lab_topology --topo resources/lab_topology_schema.json
  python manage.py import_lab_topology --topo my_topo.json --site resources/ocs_photonic_site.json
  python manage.py import_lab_topology --topo my_topo.json --name "Example lab" --replace
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from connect.models import Device, KeysightChassis, LabTopology, LabTopologyLink, LabTopologyNode

_CABLE_COLORS = {
    'dac': '#ef4444',
    'optic': '#22c55e',
    'direct': '#3b82f6',
    'ocs': '#fb923c',
}

_NODE_TYPE_FROM_VENDOR = {
    'arista': 'switch',
    'sonic': 'switch',
    'keysight': 'chassis',
    'ocs': 'ocs',
    'fortigate': 'firewall',
    'paloalto': 'firewall',
}


def _load_json(path: str) -> Dict[str, Any]:
    abs_path = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
    with open(abs_path) as fh:
        return json.load(fh)


def _build_ocs_ip_map(site_data: Dict) -> Dict[str, Dict]:
    """Build a map of ip -> device info from the OCS site JSON."""
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


def _resolve_device(ip: str, node_type: str, label: str):
    """Resolve IP to a Device or KeysightChassis DB record."""
    if not ip:
        return None, None
    device = Device.objects.filter(ip_address=ip).first()
    if device:
        return device, None
    if node_type == 'chassis':
        chassis = KeysightChassis.objects.filter(ip_address=ip).first()
        if chassis:
            return None, chassis
    return None, None


class Command(BaseCommand):
    help = 'Import a lab topology from a layout JSON file into the Lab Topology Designer'

    def add_arguments(self, parser):
        parser.add_argument(
            '--topo', required=True,
            help='Path to the topology layout JSON file',
        )
        parser.add_argument(
            '--site', default='',
            help='Path to the OCS site JSON file (optional, for device correlation)',
        )
        parser.add_argument(
            '--name', default='',
            help='Override the topology name (defaults to the label in the JSON)',
        )
        parser.add_argument(
            '--replace', action='store_true',
            help='Replace an existing topology with the same name (delete + recreate)',
        )
        parser.add_argument(
            '--update', default=0, type=int,
            help='Update an existing topology by its ID (clear nodes + re-import)',
        )

    def handle(self, *args, **options):
        topo_path = options['topo']
        site_path = options['site']

        self.stdout.write(self.style.HTTP_INFO(f'Loading topology JSON: {topo_path}'))
        try:
            topo_data = _load_json(topo_path)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise CommandError(f'Cannot read topology JSON: {exc}')

        site_data: Dict = {}
        if site_path:
            self.stdout.write(self.style.HTTP_INFO(f'Loading site JSON: {site_path}'))
            try:
                site_data = _load_json(site_path)
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                raise CommandError(f'Cannot read site JSON: {exc}')
        elif topo_data.get('site_json'):
            # Try relative to topology file
            base = os.path.dirname(os.path.abspath(topo_path))
            candidate = os.path.join(base, topo_data['site_json'])
            if os.path.exists(candidate):
                try:
                    site_data = _load_json(candidate)
                    self.stdout.write(f'  Auto-loaded site JSON: {candidate}')
                except Exception:
                    pass

        site_ip_map = _build_ocs_ip_map(site_data)

        name = options['name'] or topo_data.get('label') or 'Imported Topology'
        description = topo_data.get('description') or ''

        # Handle --update
        if options['update']:
            try:
                topo = LabTopology.objects.get(pk=options['update'])
                topo.nodes.all().delete()
                topo.name = name
                topo.description = description
                topo.source = 'import'
                topo.save()
                self.stdout.write(f'  Updating existing topology id={topo.pk}')
            except LabTopology.DoesNotExist:
                raise CommandError(f'Topology id={options["update"]} not found')
        elif options['replace']:
            LabTopology.objects.filter(name=name).delete()
            topo = LabTopology.objects.create(
                name=name, description=description, source='import',
            )
            self.stdout.write(f'  Replaced topology id={topo.pk}')
        else:
            topo = LabTopology.objects.create(
                name=name, description=description, source='import',
            )
            self.stdout.write(f'  Created topology id={topo.pk}')

        # Consolidated v3 export embedded as full file
        if topo_data.get('format') == 'labvault.lab_topology' or topo_data.get('version', 0) >= 3:
            from connect.lab_topology_io import import_topology

            if options['update']:
                topo.nodes.all().delete()
            msg = import_topology(topo, topo_data, site_data=site_data or None)
            self.stdout.write(self.style.SUCCESS(f'\n{msg}\nTopology id={topo.pk}'))
            return

        layout = topo_data.get('layout') or {}
        nodes_raw = layout.get('nodes') or []
        links_raw = layout.get('links') or []

        profile = (topo_data.get('addressing_profile') or '').strip().lower()
        if profile:
            extra = dict(topo.extra or {})
            extra['addressing_profile'] = profile
            topo.extra = extra
            topo.save(update_fields=['extra'])

        id_map: Dict[str, LabTopologyNode] = {}

        for nd in nodes_raw:
            node_id = str(nd.get('id') or nd.get('label') or 'node')
            label = nd.get('label') or node_id
            node_type = nd.get('node_type') or 'generic'
            device_ip = nd.get('device_ip') or ''

            # Supplement node info from site JSON
            site_info = site_ip_map.get(device_ip, {})
            if not node_type or node_type == 'generic':
                node_type = site_info.get('node_type', node_type)

            device, chassis = _resolve_device(device_ip, node_type, label)

            extra = dict(nd.get('extra') or {})
            if site_info:
                extra.setdefault('site_name', site_info.get('name', ''))
            from connect.lab_topology_io import _enrich_extra_from_site

            _enrich_extra_from_site(
                extra, device_ip, site_info,
                addressing_profile=(topo.extra or {}).get('addressing_profile', '') or profile,
            )
            if chassis:
                extra['chassis_type'] = chassis.chassis_type
                extra['chassis_id'] = chassis.pk
            ports = list(nd.get('ports') or [])
            if ports:
                extra['ports'] = ports

            n_obj = LabTopologyNode.objects.create(
                topology=topo,
                device=device,
                node_key=node_id,
                node_type=node_type,
                label=label,
                x=float(nd.get('x') or 0),
                y=float(nd.get('y') or 0),
                extra=extra,
            )
            id_map[node_id] = n_obj
            device_hint = f' [{device_ip}]' if device_ip else ''
            self.stdout.write(f'    Node: {label} ({node_type}){device_hint}')

        link_count = 0
        for lk in links_raw:
            if lk.get('_comment'):
                pass  # skip comment-only entries
            a_id = str(lk.get('from') or lk.get('node_a') or '')
            b_id = str(lk.get('to') or lk.get('node_b') or '')
            na = id_map.get(a_id)
            nb = id_map.get(b_id)
            if not na or not nb:
                self.stdout.write(
                    self.style.WARNING(f'    Skipping link {a_id}→{b_id}: node not found')
                )
                continue
            cable_type = lk.get('cable_type') or 'dac'
            color = lk.get('color') or _CABLE_COLORS.get(cable_type, '#888')
            label_text = lk.get('label') or ''
            port_range_a = lk.get('port_range_a') or ''
            port_range_b = lk.get('port_range_b') or ''
            count = lk.get('count') or 0

            # Build label if not provided
            if not label_text and count:
                cable_name = 'optic' if cable_type == 'optic' else 'DAC'
                label_text = f'{count}× {cable_name}'
                if port_range_a or port_range_b:
                    label_text += f' · {port_range_a}↔{port_range_b}'

            link_extra = {}
            if port_range_a:
                link_extra['port_range_a'] = port_range_a
            if port_range_b:
                link_extra['port_range_b'] = port_range_b
            if count:
                link_extra['count'] = count

            LabTopologyLink.objects.create(
                topology=topo,
                node_a=na,
                port_a=port_range_a or '',
                node_b=nb,
                port_b=port_range_b or '',
                cable_type=cable_type,
                color=color,
                label=label_text,
                extra=link_extra,
            )
            link_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'\nImported {len(nodes_raw)} nodes and {link_count} links.\n'
                f'Topology id={topo.pk} — view at /lab-topology/{topo.pk}/'
            )
        )
