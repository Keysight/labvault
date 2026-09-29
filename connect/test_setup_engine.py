"""
Test Setup Engine — Phase 2 + 3
Provides ResourceCalculator (compute patch/switch plans) and SetupExecutor (apply them).

``ResourceCalculator.compute`` is pure planning: it reads the topology and
returns a ``computed_plan`` dict that the views store on ``TestSetupTemplate``.
``SetupExecutor.execute`` runs in a daemon thread started by
``test_setup_apply`` and mutates hardware: OCS ``xconnect_add`` via the OCS
driver's ``send_config`` and switch CLI via ``send_config``. Progress is written
to ``TestSetupRun.steps`` / ``log`` after every step.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from django.utils import timezone

logger = logging.getLogger(__name__)

_PORT_SPEEDS = ['100G', '200G', '400G', '800G', '1.6T', '25G', '10G', '40G']


# ─────────────────────────────────────────────────────────────────────────────
# Resource Calculator
# ─────────────────────────────────────────────────────────────────────────────

class ResourceCalculator:
    """
    Computes OCS patches, switch configs, and chassis port assignments
    for a given test topology and requirements.
    """

    def compute(self, topo, requirements: Dict) -> Dict:
        """
        Args:
            topo: LabTopology model instance
            requirements: {
                chassis_node_ids: [int, ...],
                port_count: int,
                port_speed: "400G",
                test_type: "full_mesh" | "p2p" | "one_to_many",
            }
        Returns:
            computed_plan: {
                ocs_patches: [{a, b, xc_name, port_a_triplet, port_b_triplet}],
                switch_configs: {arista_ip: [cli_line, ...]},
                chassis_ports: {chassis_ip: [port_label, ...]},
                summary: {patch_count, chassis_count, port_count, speed, test_type},
                warnings: [str],
            }
        """
        from .models import LabTopologyNode, KeysightChassis, Device

        chassis_node_ids = [int(x) for x in (requirements.get('chassis_node_ids') or [])]
        port_count = int(requirements.get('port_count') or 8)
        port_speed = str(requirements.get('port_speed') or '400G')
        test_type = str(requirements.get('test_type') or 'full_mesh')

        if not chassis_node_ids:
            raise ValueError('chassis_node_ids is required')

        # Load chassis nodes
        chassis_nodes = list(topo.nodes.filter(pk__in=chassis_node_ids, node_type='chassis')
                             .select_related('device'))
        if not chassis_nodes:
            raise ValueError('No chassis nodes found for the given ids')

        # Build OCS triplet map for this topology's OCS device
        triplet_map = self._build_triplet_map(topo)
        warnings: List[str] = []

        # Assign chassis ports (up to port_count per chassis)
        chassis_ports: Dict[str, List[str]] = {}
        chassis_triplets: Dict[str, List[str]] = {}  # chassis_ip -> [triplet, ...]

        for node in chassis_nodes:
            ip = node.device.ip_address if node.device else (node.extra or {}).get('device_ip', '')
            if not ip:
                warnings.append(f'Node {node.label} has no IP — skipping')
                continue
            # Get OCS triplets mapped to this chassis
            triplets = triplet_map.get(ip, [])
            if not triplets:
                warnings.append(f'{node.label} ({ip}) has no OCS triplet mapping')
            selected_triplets = triplets[:port_count]
            chassis_triplets[ip] = selected_triplets
            # Port labels from node.extra.ports or generic
            avail_ports = list((node.extra or {}).get('ports') or [])
            chassis_ports[ip] = avail_ports[:port_count] or [f'port_{i+1}' for i in range(port_count)]

        # Generate OCS cross-connect pairs
        ocs_patches: List[Dict] = []
        chassis_ips = list(chassis_triplets.keys())

        if test_type == 'full_mesh':
            pairs = [(chassis_ips[i], chassis_ips[j])
                     for i in range(len(chassis_ips))
                     for j in range(i + 1, len(chassis_ips))]
        elif test_type == 'p2p':
            pairs = [(chassis_ips[0], chassis_ips[1])] if len(chassis_ips) >= 2 else []
        elif test_type == 'one_to_many':
            src = chassis_ips[0]
            pairs = [(src, dst) for dst in chassis_ips[1:]]
        else:
            pairs = []

        patch_idx = 0
        for ip_a, ip_b in pairs:
            triplets_a = chassis_triplets.get(ip_a, [])
            triplets_b = chassis_triplets.get(ip_b, [])
            min_ports = min(len(triplets_a), len(triplets_b), port_count)
            for i in range(min_ports):
                ta, tb = triplets_a[i], triplets_b[i]
                patch_idx += 1
                ocs_patches.append({
                    'a': ta,
                    'b': tb,
                    'xc_name': f'LV_XC_{patch_idx:03d}_{ip_a.split(".")[-1]}_{ip_b.split(".")[-1]}',
                    'chassis_a': ip_a,
                    'chassis_b': ip_b,
                    'port_a_triplet': ta,
                    'port_b_triplet': tb,
                })

        # Generate Arista switch configs (VLANs for loopback/DAC connections)
        switch_configs = self._generate_switch_configs(topo, chassis_nodes, port_speed)

        summary = {
            'patch_count': len(ocs_patches),
            'chassis_count': len(chassis_nodes),
            'port_count': port_count,
            'speed': port_speed,
            'test_type': test_type,
            'pair_count': len(pairs),
        }

        return {
            'ocs_patches': ocs_patches,
            'switch_configs': switch_configs,
            'chassis_ports': chassis_ports,
            'summary': summary,
            'warnings': warnings,
        }

    def _build_triplet_map(self, topo) -> Dict[str, List[str]]:
        """Map chassis IP → list of OCS triplet strings from topology link extra data."""
        from .ocs_helpers import load_ocs_site_triplets

        # Try to find an OCS device from this topology
        ocs_node = topo.nodes.filter(node_type='ocs').select_related('device').first()
        if not ocs_node or not ocs_node.device:
            return {}
        try:
            result = load_ocs_site_triplets(ocs_node.device)
            return result  # {ip: [triplets]}
        except Exception as exc:
            logger.warning('Could not load OCS triplet map: %s', exc)
            return {}

    def _generate_switch_configs(self, topo, chassis_nodes, port_speed: str) -> Dict[str, List[str]]:
        """Generate minimal Arista config snippets for the test setup."""
        from .models import LabTopologyNode

        switch_nodes = list(topo.nodes.filter(node_type='switch').select_related('device'))
        configs: Dict[str, List[str]] = {}

        speed_to_vlan = {
            '100G': 100, '200G': 200, '400G': 400, '800G': 800,
        }
        vlan_id = speed_to_vlan.get(port_speed, 100)

        for sw_node in switch_nodes:
            if not sw_node.device:
                continue
            sw_ip = sw_node.device.ip_address
            cmds = [
                f'! LabVault auto-config for {port_speed} test',
                f'vlan {vlan_id}',
                f'  name LV_TEST_{port_speed}',
                '!',
            ]
            # Add interface stanzas for links to chassis nodes in selection
            selected_chassis_ips = {
                n.device.ip_address for n in chassis_nodes if n.device
            }
            for lk in topo.links.filter(node_a=sw_node).select_related('node_b__device'):
                if lk.node_b.node_type == 'chassis' and lk.node_b.device:
                    if lk.node_b.device.ip_address in selected_chassis_ips:
                        port_range = (lk.extra or {}).get('port_range_a') or lk.port_a
                        cmds += [
                            f'interface Ethernet{port_range}',
                            f'  switchport mode access',
                            f'  switchport access vlan {vlan_id}',
                            '!',
                        ]
            for lk in topo.links.filter(node_b=sw_node).select_related('node_a__device'):
                if lk.node_a.node_type == 'chassis' and lk.node_a.device:
                    if lk.node_a.device.ip_address in selected_chassis_ips:
                        port_range = (lk.extra or {}).get('port_range_b') or lk.port_b
                        cmds += [
                            f'interface Ethernet{port_range}',
                            f'  switchport mode access',
                            f'  switchport access vlan {vlan_id}',
                            '!',
                        ]
            if len(cmds) > 4:
                configs[sw_ip] = cmds

        return configs


# ─────────────────────────────────────────────────────────────────────────────
# Setup Executor
# ─────────────────────────────────────────────────────────────────────────────

class SetupExecutor:
    """
    Applies a computed_plan from TestSetupTemplate:
      1. OCS cross-connect patches (xconnect_add)
      2. Arista switch configs (send_config)
    """

    def execute(self, run) -> None:
        """Entry point — called in a background thread."""
        from .models import TestSetupRun

        run.status = 'running'
        run.started_at = timezone.now()
        run.save(update_fields=['status', 'started_at'])

        plan = run.template.computed_plan
        steps: List[Dict] = []
        log_lines: List[str] = []

        def _step(name: str, state: str, message: str = ''):
            ts = datetime.now().isoformat()
            steps.append({'name': name, 'state': state, 'message': message, 'ts': ts})
            log_lines.append(f'[{ts}] [{state}] {name}: {message}')
            run.steps = steps
            run.log = '\n'.join(log_lines)
            run.save(update_fields=['steps', 'log'])

        try:
            # Step 1: OCS Patches
            ocs_patches = plan.get('ocs_patches') or []
            _step('OCS Patches', 'running', f'{len(ocs_patches)} patch(es) to apply')
            ocs_results = self._apply_ocs_patches(plan, run.template.topology)
            _step('OCS Patches', 'ok', ocs_results)

            # Step 2: Switch configs
            switch_configs = plan.get('switch_configs') or {}
            _step('Switch Configs', 'running', f'{len(switch_configs)} switch(es) to configure')
            sw_results = self._apply_switch_configs(switch_configs)
            _step('Switch Configs', 'ok', sw_results)

            run.status = 'applied'
            run.result_summary = {'ocs': ocs_results, 'switch': sw_results}
        except Exception as exc:
            logger.exception('SetupExecutor.execute run=%s', run.pk)
            _step('Error', 'error', str(exc))
            run.status = 'error'
        finally:
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'finished_at', 'steps', 'log', 'result_summary'])
            run.template.status = run.status if run.status == 'error' else 'applied'
            run.template.save(update_fields=['status', 'updated_at'])

    def _apply_ocs_patches(self, plan: Dict, topo) -> str:
        from .models import Device

        ocs_node = topo.nodes.filter(node_type='ocs').select_related('device').first() if topo else None
        if not ocs_node or not ocs_node.device:
            return 'No OCS device found in topology — patches skipped'

        ocs_device = ocs_node.device
        patches = plan.get('ocs_patches') or []
        if not patches:
            return 'No patches to apply'

        from .drivers import get_driver
        driver = get_driver(ocs_device)
        applied = 0
        errors = []
        for p in patches:
            try:
                import json as _json
                # OcsDriver.send_config expects {op, in, out, conn} (REST xconnect_add).
                result = driver.send_config([_json.dumps({
                    'op': 'xconnect_add',
                    'in': p['a'],
                    'out': p['b'],
                    'conn': p.get('xc_name', ''),
                })])
                if result.success:
                    applied += 1
                else:
                    errors.append(f"{p['a']}↔{p['b']}: {result.error}")
            except Exception as exc:
                errors.append(f"{p['a']}↔{p['b']}: {exc}")

        msg = f'Applied {applied}/{len(patches)} OCS patches'
        if errors:
            msg += '; Errors: ' + '; '.join(errors[:5])
        return msg

    def _apply_switch_configs(self, switch_configs: Dict) -> str:
        from .models import Device
        from .drivers import get_driver

        applied = 0
        errors = []
        for ip, cmds in switch_configs.items():
            device = Device.objects.filter(ip_address=ip).first()
            if not device:
                errors.append(f'{ip}: device not in DB')
                continue
            try:
                driver = get_driver(device)
                result = driver.send_config(cmds)
                if result.success:
                    applied += 1
                else:
                    errors.append(f'{ip}: {result.error}')
            except Exception as exc:
                errors.append(f'{ip}: {exc}')

        msg = f'Configured {applied}/{len(switch_configs)} switch(es)'
        if errors:
            msg += '; Errors: ' + '; '.join(errors[:5])
        return msg
