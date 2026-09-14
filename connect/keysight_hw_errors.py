"""Per-node hardware error flags (CN / mgmt) via KeysightBmcEndpoint."""

from __future__ import annotations

from django.utils import timezone

from .models import KeysightBmcEndpoint, KeysightChassis


def _flag_dict(ep: KeysightBmcEndpoint) -> dict:
    return {
        'hardware_error_reported': ep.hardware_error_reported,
        'hardware_error_notes': ep.hardware_error_notes or '',
        'bmc_hostname': ep.hostname,
    }


def bmc_hw_flags_for_chassis(chassis_id: int) -> dict[str, dict]:
    """Map node_name (lower) and bmc hostname (lower) → flag dict."""
    out: dict[str, dict] = {}
    for ep in KeysightBmcEndpoint.objects.filter(chassis_id=chassis_id):
        data = _flag_dict(ep)
        if ep.node_name:
            out[ep.node_name.lower()] = data
        if ep.hostname:
            out[ep.hostname.lower()] = data
    return out


def lookup_node_hw_flag(flags: dict[str, dict], *, node_name: str = '', bmc_hostname: str = '') -> dict:
    for key in (bmc_hostname, node_name):
        k = (key or '').strip().lower()
        if k and k in flags:
            return flags[k]
    return {'hardware_error_reported': False, 'hardware_error_notes': '', 'bmc_hostname': ''}


def apply_hw_flags_to_cards(cards: list, chassis_id: int) -> list:
    flags = bmc_hw_flags_for_chassis(chassis_id)
    enriched = []
    for card in cards:
        row = dict(card)
        hw = lookup_node_hw_flag(
            flags,
            node_name=row.get('node_name', ''),
            bmc_hostname=row.get('bmc_name', '') or row.get('bmc_hostname', ''),
        )
        row['hardware_error_reported'] = hw['hardware_error_reported']
        row['hardware_error_notes'] = hw['hardware_error_notes']
        enriched.append(row)
    return enriched


def enrich_assoc_hw_flags(by_id: dict[int, dict], chassis_ids: list[int] | None = None) -> None:
    """Attach per-node hardware error flags to node association dicts."""
    qs = KeysightBmcEndpoint.objects.exclude(node_name='')
    if chassis_ids is not None:
        qs = qs.filter(chassis_id__in=chassis_ids)
    by_chassis_node: dict[tuple[int, str], KeysightBmcEndpoint] = {}
    by_hostname: dict[str, KeysightBmcEndpoint] = {}
    for ep in qs:
        if ep.chassis_id and ep.node_name:
            by_chassis_node[(ep.chassis_id, ep.node_name.lower())] = ep
        if ep.hostname:
            by_hostname[ep.hostname.lower()] = ep

    def _apply(node: dict, chassis_id: int) -> None:
        ep = None
        bmc_hn = (node.get('bmc_hostname') or '').lower()
        if bmc_hn:
            ep = by_hostname.get(bmc_hn)
        if not ep:
            ep = by_chassis_node.get((chassis_id, (node.get('name') or '').lower()))
        if ep:
            node['hardware_error_reported'] = ep.hardware_error_reported
            node['hardware_error_notes'] = ep.hardware_error_notes or ''
            node['bmc_hostname'] = node.get('bmc_hostname') or ep.hostname
        else:
            node.setdefault('hardware_error_reported', False)
            node.setdefault('hardware_error_notes', '')

    for cid, assoc in by_id.items():
        if assoc.get('standalone'):
            _apply(assoc.get('mgmt_node') or {}, cid)
            continue
        mgmt = assoc.get('mgmt_node')
        if mgmt:
            _apply(mgmt, cid)
        for cn in assoc.get('compute_nodes') or []:
            _apply(cn, cid)


def chassis_mgmt_hw_bad(ch: KeysightChassis, assoc: dict | None) -> bool:
    """Chassis/mgmt bad bucket: edit-chassis flag or explicitly flagged mgmt node only."""
    if ch.hardware_error_reported:
        return True
    if not assoc:
        return False
    mgmt = assoc.get('mgmt_node') or {}
    return bool(mgmt.get('hardware_error_reported'))


def chassis_shows_hw_warning(ch: KeysightChassis, assoc: dict | None) -> bool:
    """Row warning: any reported issue on this chassis (incl. CNs)."""
    if chassis_mgmt_hw_bad(ch, assoc):
        return True
    if not assoc:
        return False
    return any(cn.get('hardware_error_reported') for cn in assoc.get('compute_nodes') or [])


def count_hw_error_units(chassis_list, node_by_id: dict | None) -> int:
    """Bad assignable units: flagged CNs/mgmt nodes + chassis-wide flags without per-node flags."""
    node_by_id = node_by_id or {}
    total = 0
    for ch in chassis_list:
        assoc = node_by_id.get(ch.id) or {}
        node_flagged = False
        if assoc.get('standalone'):
            if ch.hardware_error_reported or (assoc.get('mgmt_node') or {}).get('hardware_error_reported'):
                total += 1
            continue
        for cn in assoc.get('compute_nodes') or []:
            if cn.get('hardware_error_reported'):
                total += 1
                node_flagged = True
        mgmt = assoc.get('mgmt_node') or {}
        if mgmt.get('hardware_error_reported'):
            total += 1
            node_flagged = True
        if ch.hardware_error_reported and not node_flagged:
            total += 1
    return total


def build_selection_hw_summary(chassis_list, node_by_id: dict | None) -> dict:
    """Totals and bad-unit counts for dashboard top bar (chassis · CN1.5 · CN1.0)."""
    from .keysight_aps_generations import (
        APS_GEN_10,
        APS_GEN_15,
        _count_compute_nodes_in_selection,
        _count_standalone_cn_in_selection,
        node_aps_generation,
        standalone_gen_for_chassis,
    )

    node_by_id = node_by_id or {}
    cn15_total = (
        _count_compute_nodes_in_selection(chassis_list, node_by_id, aps_gen=APS_GEN_15)
        + _count_standalone_cn_in_selection(chassis_list, node_by_id, aps_gen=APS_GEN_15)
    )
    cn10_total = (
        _count_compute_nodes_in_selection(chassis_list, node_by_id, aps_gen=APS_GEN_10)
        + _count_standalone_cn_in_selection(chassis_list, node_by_id, aps_gen=APS_GEN_10)
    )
    cn15_bad = cn10_bad = 0
    for ch in chassis_list:
        assoc = node_by_id.get(ch.id) or {}
        if ch.chassis_type == 'aps_standalone' or assoc.get('standalone'):
            mgmt = assoc.get('mgmt_node') or {}
            if ch.hardware_error_reported or mgmt.get('hardware_error_reported'):
                gen = standalone_gen_for_chassis(ch, assoc)
                if gen == APS_GEN_15:
                    cn15_bad += 1
                elif gen == APS_GEN_10:
                    cn10_bad += 1
            continue
        for cn in assoc.get('compute_nodes') or []:
            if not cn.get('hardware_error_reported'):
                continue
            gen = node_aps_generation(
                cn.get('name', ''), cn.get('role', ''),
                chassis_hostname=ch.hostname or '',
                fru_board_product=cn.get('fru_board_product', ''),
                aps_gen_hint=cn.get('aps_gen', ''),
            )
            if gen == APS_GEN_15:
                cn15_bad += 1
            elif gen == APS_GEN_10:
                cn10_bad += 1

    chassis_bad = sum(
        1 for ch in chassis_list
        if chassis_mgmt_hw_bad(ch, node_by_id.get(ch.id))
    )
    return {
        'chassis_total': len(chassis_list),
        'chassis_bad': chassis_bad,
        'cn15_total': cn15_total,
        'cn15_bad': cn15_bad,
        'cn10_total': cn10_total,
        'cn10_bad': cn10_bad,
    }


def enrich_type_breakdown_hw_bad(
    rows: list[dict],
    chassis_list,
    node_by_id: dict | None,
) -> list[dict]:
    """Attach ``bad_count`` to model/CN chips in the selection summary."""
    from .keysight_aps_generations import (
        APS_GEN_10,
        APS_GEN_15,
        node_aps_generation,
        standalone_gen_for_chassis,
    )

    node_by_id = node_by_id or {}

    def _bad_compute(aps_gen: str) -> int:
        total = 0
        for ch in chassis_list:
            assoc = node_by_id.get(ch.id) or {}
            if ch.chassis_type == 'aps_standalone' or assoc.get('standalone'):
                continue
            for cn in assoc.get('compute_nodes') or []:
                if not cn.get('hardware_error_reported'):
                    continue
                if node_aps_generation(
                    cn.get('name', ''), cn.get('role', ''),
                    chassis_hostname=ch.hostname or '',
                    fru_board_product=cn.get('fru_board_product', ''),
                    aps_gen_hint=cn.get('aps_gen', ''),
                ) == aps_gen:
                    total += 1
        return total

    def _bad_standalone(aps_gen: str) -> int:
        total = 0
        for ch in chassis_list:
            assoc = node_by_id.get(ch.id) or {}
            if ch.chassis_type != 'aps_standalone' and not assoc.get('standalone'):
                continue
            mgmt = assoc.get('mgmt_node') or {}
            if not (ch.hardware_error_reported or mgmt.get('hardware_error_reported')):
                continue
            if standalone_gen_for_chassis(ch, assoc) == aps_gen:
                total += 1
        return total

    cn_bad = {
        'CN 1.5': _bad_compute(APS_GEN_15),
        'CN 1.0': _bad_compute(APS_GEN_10),
        'Standalone CN 1.5': _bad_standalone(APS_GEN_15),
        'Standalone CN 1.0': _bad_standalone(APS_GEN_10),
    }
    out = []
    for row in rows:
        item = dict(row)
        if item.get('row_kind') == 'chassis':
            label = item.get('label', '')
            item['bad_count'] = sum(
                1 for ch in chassis_list
                if ch.get_chassis_type_display() == label
                and chassis_mgmt_hw_bad(ch, node_by_id.get(ch.id))
            )
        elif item.get('label') in cn_bad:
            item['bad_count'] = cn_bad[item['label']]
        else:
            item['bad_count'] = 0
        out.append(item)
    return out


def resolve_bmc_endpoint(
    ch: KeysightChassis,
    *,
    node_name: str = '',
    bmc_hostname: str = '',
) -> KeysightBmcEndpoint | None:
    node_name = (node_name or '').strip()
    bmc_hostname = (bmc_hostname or '').strip()
    if bmc_hostname:
        ep = KeysightBmcEndpoint.objects.filter(hostname=bmc_hostname).first()
        if ep:
            if node_name and not ep.node_name:
                ep.node_name = node_name
                ep.chassis = ch
                ep.save(update_fields=['node_name', 'chassis'])
            return ep
    if node_name:
        ep = KeysightBmcEndpoint.objects.filter(chassis=ch, node_name=node_name).first()
        if ep:
            return ep
    if bmc_hostname:
        return KeysightBmcEndpoint.objects.create(
            hostname=bmc_hostname,
            chassis=ch,
            node_name=node_name,
            source='chassis_derived',
            last_seen=timezone.now(),
        )
    if node_name:
        placeholder = f'{ch.id}:{node_name}'.lower()
        return KeysightBmcEndpoint.objects.get_or_create(
            hostname=placeholder,
            defaults={
                'chassis': ch,
                'node_name': node_name,
                'source': 'manual_import',
            },
        )[0]
    return None


def collect_hw_error_report() -> list[dict]:
    """All chassis-wide and per-node hardware error flags for Slack / APIs."""
    rows: list[dict] = []
    for ch in KeysightChassis.objects.filter(hardware_error_reported=True).order_by('hostname', 'ip_address'):
        rows.append({
            'kind': 'chassis',
            'chassis_id': ch.id,
            'chassis_label': ch.hostname or ch.ip_address,
            'node_name': '',
            'notes': ch.notes or '',
        })
    for ep in KeysightBmcEndpoint.objects.filter(
        hardware_error_reported=True,
    ).select_related('chassis').order_by('chassis__hostname', 'node_name'):
        ch = ep.chassis
        rows.append({
            'kind': 'node',
            'chassis_id': ch.id if ch else None,
            'chassis_label': (ch.hostname or ch.ip_address) if ch else '(unassigned)',
            'node_name': ep.node_name or ep.hostname,
            'notes': ep.hardware_error_notes or '',
        })
    return rows
