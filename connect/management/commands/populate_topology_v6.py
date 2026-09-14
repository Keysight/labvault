"""
Sync IPv6 management addresses from a v6 lab topology into inventory and refresh port layouts.

Writes static ``mgmt_ipv6`` on KeysightChassis / Device rows (required for IPv6 connect),
enriches node ``extra`` via ``mgmt_ip_bundle``, and optionally refreshes port-fabric cache.

  python manage.py populate_topology_v6 --topo-id 25
  python manage.py populate_topology_v6 --topo-id 25 --dry-run
  python manage.py populate_topology_v6 --topo-id 25 --skip-fabric
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from connect.ip_addressing import derive_dhcpv6_ocs_lab, is_ipv4, mgmt_ip_bundle, normalize_ip
from connect.lab_topology_views import refresh_port_fabric_snapshot
from connect.models import Device, KeysightChassis, LabTopology, LabTopologyNode
from connect.topology_dac_finder import enrich_topology_ports_from_cache
from connect.topology_fabric_cache import invalidate_topology_fabric_cache


def _ipv4_for_node(node: LabTopologyNode) -> str:
    ex = node.extra or {}
    v4 = (ex.get('device_ip') or '').strip()
    if not v4 and node.device_id and node.device:
        v4 = (node.device.ip_address or '').strip()
    if v4 and is_ipv4(v4):
        return normalize_ip(v4) or v4
    cid = ex.get('chassis_id')
    if cid:
        ch = KeysightChassis.objects.filter(pk=cid).only('ip_address').first()
        if ch:
            return (ch.ip_address or '').strip()
    return ''


def _ipv6_for_node(node: LabTopologyNode, ipv4: str) -> str:
    ex = node.extra or {}
    v6 = normalize_ip(ex.get('mgmt_ipv6') or '')
    if v6:
        return v6
    if node.device_id and node.device:
        v6 = normalize_ip(node.device.mgmt_ipv6 or '')
        if v6:
            return v6
    cid = ex.get('chassis_id')
    if cid:
        ch = KeysightChassis.objects.filter(pk=cid).only('mgmt_ipv6', 'ip_address').first()
        if ch and ch.mgmt_ipv6:
            return normalize_ip(ch.mgmt_ipv6)
    return derive_dhcpv6_ocs_lab(ipv4)


def _sync_chassis(ch: KeysightChassis, v6: str, *, dry_run: bool) -> bool:
    if not v6:
        return False
    changed = (
        ch.mgmt_ipv6 != v6
        or ch.mgmt_ipv6_source != 'static'
        or ch.preferred_ip_version != 'ipv6'
    )
    if not changed:
        return False
    if dry_run:
        return True
    ch.mgmt_ipv6 = v6
    ch.mgmt_ipv6_source = 'static'
    ch.preferred_ip_version = 'ipv6'
    ch.save(update_fields=['mgmt_ipv6', 'mgmt_ipv6_source', 'preferred_ip_version', 'updated_at'])
    return True


def _sync_device(dev: Device, v6: str, *, dry_run: bool) -> bool:
    if not v6:
        return False
    changed = (
        dev.mgmt_ipv6 != v6
        or dev.mgmt_ipv6_source != 'static'
        or dev.preferred_ip_version != 'ipv6'
    )
    if not changed:
        return False
    if dry_run:
        return True
    dev.mgmt_ipv6 = v6
    dev.mgmt_ipv6_source = 'static'
    dev.preferred_ip_version = 'ipv6'
    dev.save(update_fields=['mgmt_ipv6', 'mgmt_ipv6_source', 'preferred_ip_version', 'updated_at'])
    return True


class Command(BaseCommand):
    help = 'Apply topology v6 addresses to inventory, enrich ports, and warm port-fabric cache.'

    def add_arguments(self, parser):
        parser.add_argument('--topo-id', type=int, required=True)
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument(
            '--skip-enrich',
            action='store_true',
            help='Skip enrich_topology_ports_from_cache (no chassis SSH)',
        )
        parser.add_argument('--skip-fabric', action='store_true', help='Skip port-fabric snapshot rebuild')
        parser.add_argument(
            '--no-refresh-chassis',
            action='store_true',
            help='Enrich from cache only (no live chassis fetch)',
        )

    def handle(self, *args, **options):
        topo_id = options['topo_id']
        dry = options['dry_run']
        topo = LabTopology.objects.filter(pk=topo_id).first()
        if not topo:
            raise CommandError(f'Topology {topo_id} not found')

        extra_topo = dict(topo.extra or {})
        if extra_topo.get('addressing_profile') != 'v6':
            if dry:
                self.stdout.write('  [topo] would set addressing_profile=v6')
            else:
                extra_topo['addressing_profile'] = 'v6'
                topo.extra = extra_topo
                topo.save(update_fields=['extra', 'updated_at'])

        nodes = list(
            LabTopologyNode.objects.filter(topology=topo).select_related('device'),
        )
        node_updates = 0
        chassis_updates = 0
        device_updates = 0

        for node in nodes:
            v4 = _ipv4_for_node(node)
            v6 = _ipv6_for_node(node, v4)
            ex = dict(node.extra or {})
            pref = ex.get('preferred_ip_version') or 'ipv6'
            if node.node_type in ('server',):
                pref = ex.get('preferred_ip_version') or 'ipv4'
            bundle = mgmt_ip_bundle(
                ipv4=v4,
                ipv6=v6 or ex.get('mgmt_ipv6', ''),
                preferred=pref,
                hostname=ex.get('hostname', '') or (node.device.hostname if node.device else ''),
                ipv6_source='static' if v6 else (ex.get('mgmt_ipv6_source') or ''),
            )
            merged = {**ex, **bundle}
            if v6:
                merged['mgmt_ipv6'] = v6
                merged['mgmt_ipv6_source'] = 'static'
                merged['preferred_ip_version'] = 'ipv6'
            if node.node_type == 'chassis' and v4:
                cid = merged.get('chassis_id')
                if not cid:
                    ch = KeysightChassis.objects.filter(ip_address=v4).first()
                    if ch:
                        merged['chassis_id'] = ch.pk
                        cid = ch.pk
                if cid:
                    ch = KeysightChassis.objects.filter(pk=cid).first()
                    if ch and _sync_chassis(ch, v6, dry_run=dry):
                        chassis_updates += 1
                        self.stdout.write(
                            f'  chassis {ch.ip_address} → {v6} (connect {ch.connect_address if not dry else "…"})',
                        )
            if node.device_id and node.device and v6:
                if _sync_device(node.device, v6, dry_run=dry):
                    device_updates += 1
                    self.stdout.write(
                        f'  device {node.device.ip_address} → {v6}',
                    )
            if merged != ex:
                node_updates += 1
                if dry:
                    self.stdout.write(f'  [node] {node.label}: device_ip={bundle.get("device_ip")} display={bundle.get("mgmt_display")}')
                else:
                    node.extra = merged
                    node.save(update_fields=['extra'])

        self.stdout.write(
            self.style.SUCCESS(
                f'{"[dry-run] " if dry else ""}Nodes {node_updates}, chassis {chassis_updates}, devices {device_updates}',
            ),
        )

        if dry:
            return

        if not options['skip_enrich']:
            self.stdout.write('Enriching topology ports (chassis may SSH when --refresh)...')
            refresh = not options['no_refresh_chassis']
            n = enrich_topology_ports_from_cache(topo, refresh=refresh)
            self.stdout.write(self.style.SUCCESS(f'  enriched {n} node(s)'))

        if not options['skip_fabric']:
            invalidate_topology_fabric_cache(topo_id)
            self.stdout.write('Rebuilding port-fabric snapshot (cold)...')
            refresh_port_fabric_snapshot(
                topo_id, want_live=False, want_lldp=True, force_refresh=True,
            )
            self.stdout.write('Rebuilding port-fabric snapshot (warm / live OCS)...')
            refresh_port_fabric_snapshot(
                topo_id, want_live=True, want_lldp=True, force_refresh=True,
            )
            self.stdout.write(self.style.SUCCESS('Port fabric cache updated.'))
