"""APS 1.0 / 1.5 grouping from KCOS node names and BMC FRU data.

APS 1.5 CNs are identified from IPMI FRU board product (e.g. APS-ONE-150) when
available, otherwise from node names (``cn-aps-o2-``, ``cn-aps-o15-``, etc.).

Classification precedence in :func:`node_aps_generation`: explicit ``aps_gen``
hint (from ``KeysightBmcEndpoint.aps_gen``) → FRU strings → node name → the
standalone hostname rule. Generation codes are the strings ``'10'`` and
``'15'``. Inputs are node-association dicts from
``keysight_node_associations`` (``mgmt_node`` / ``compute_nodes``); nothing
here touches the network or database. Used by the dashboard ``aps_gen`` filter
chips and the "models in selection" breakdown.
"""

from __future__ import annotations

import re
from collections import Counter

from .keysight_aps_standalone import (
    standalone_aps_generation,
    standalone_gen_for_chassis,
)
from .keysight_drivers import KCOS_TYPES

APS_GEN_10 = '10'
APS_GEN_15 = '15'

APS_GEN_LABELS = {
    APS_GEN_10: 'APS 1.0',
    APS_GEN_15: 'APS 1.5',
}

# Node-name patterns for APS 1.5 (O2 / O15 generations).
_APS_15_NODE_RE = re.compile(r'cn-aps-o(2|15)([-._]|$)', re.IGNORECASE)

# FRU / product strings — APS-ONE-100 = 1.0, APS-ONE-150 / O15 = 1.5, etc.
_APS_15_FRU_RE = re.compile(
    r'APS[-\s]?ONE[-\s]?1?5|APS[-\s]?O15|\bO15\b|APS[-\s]?1\.5',
    re.IGNORECASE,
)
_APS_10_FRU_RE = re.compile(
    r'APS[-\s]?ONE[-\s]?100|\bO1\b(?!5)|APS[-\s]?ONE[-\s]?10\b',
    re.IGNORECASE,
)


def is_aps_15_node_name(name: str) -> bool:
    """True for ``cn-aps-o2-*`` / ``cn-aps-o15-*`` compute-node names."""
    return bool(_APS_15_NODE_RE.search((name or '').lower()))


def is_kcos_node_name(name: str) -> bool:
    """True for any ``cn-*`` KCOS compute-node name (default generation 1.0)."""
    n = (name or '').lower()
    return n.startswith('cn-aps-') or n.startswith('cn-')


def aps_gen_from_fru(
    fru_board_product: str = '',
    fru_product_name: str = '',
    fru_board_part: str = '',
) -> str | None:
    """Classify APS generation from IPMI FRU dump fields."""
    blob = ' '.join([fru_board_product, fru_product_name, fru_board_part]).strip()
    if not blob:
        return None
    if _APS_15_FRU_RE.search(blob):
        return APS_GEN_15
    if _APS_10_FRU_RE.search(blob):
        return APS_GEN_10
    upper = blob.upper()
    if 'O2' in upper and 'O15' not in upper and 'APS' in upper:
        return APS_GEN_15
    return None


def node_aps_generation(
    name: str,
    role: str = '',
    *,
    chassis_hostname: str = '',
    fru_board_product: str = '',
    fru_product_name: str = '',
    fru_board_part: str = '',
    aps_gen_hint: str = '',
) -> str | None:
    """Classify a single mgmt/compute node; None if not a KCOS node name."""
    if aps_gen_hint in (APS_GEN_10, APS_GEN_15):
        return aps_gen_hint
    fru_gen = aps_gen_from_fru(fru_board_product, fru_product_name, fru_board_part)
    if fru_gen:
        return fru_gen
    n = (name or '').strip()
    if not n and role not in ('Management', 'Standalone'):
        return None
    if is_aps_15_node_name(n):
        return APS_GEN_15
    if is_kcos_node_name(n):
        return APS_GEN_10
    if role in ('Management', 'Standalone'):
        return standalone_aps_generation(chassis_hostname, n)
    return None


def _iter_assoc_nodes(assoc: dict | None):
    if not assoc:
        return
    mgmt = assoc.get('mgmt_node')
    if mgmt:
        yield mgmt, True
    for cn in assoc.get('compute_nodes') or []:
        yield cn, False


def chassis_has_aps_gen(assoc: dict | None, aps_gen: str) -> bool:
    """True if this chassis has at least one node of the given APS generation."""
    if assoc and assoc.get('standalone'):
        return standalone_gen_for_chassis(None, assoc) == aps_gen
    for node, _is_mgmt in _iter_assoc_nodes(assoc):
        gen = node_aps_generation(
            node.get('name', ''),
            node.get('role', ''),
            chassis_hostname=node.get('chassis_hostname', ''),
            fru_board_product=node.get('fru_board_product', ''),
            fru_product_name=node.get('fru_product_name', ''),
            aps_gen_hint=node.get('aps_gen', ''),
        )
        if gen == aps_gen:
            return True
    return False


def filter_chassis_list_by_aps_gen(
    chassis_list,
    aps_gen: str,
    node_by_id: dict[int, dict] | None,
) -> list:
    """Keep KCOS chassis that have at least one node matching ``aps_gen``."""
    if aps_gen not in APS_GEN_LABELS:
        return []
    node_by_id = node_by_id or {}
    out = []
    for ch in chassis_list:
        if ch.chassis_type not in KCOS_TYPES:
            continue
        if chassis_has_aps_gen(node_by_id.get(ch.id), aps_gen):
            out.append(ch)
    return out


def filter_chassis_queryset_by_aps_gen(qs, aps_gen: str):
    """DB-level filter is not possible; return KCOS chassis for in-memory node filter."""
    if aps_gen not in APS_GEN_LABELS:
        return qs.none()
    return qs.filter(chassis_type__in=KCOS_TYPES)


def count_nodes_for_aps_gen(assoc: dict | None, aps_gen: str) -> tuple[int, int]:
    """Return ``(mgmt, compute)`` node counts of ``aps_gen`` in one association.

    A standalone appliance counts as one mgmt node and zero compute nodes.
    """
    if assoc and assoc.get('standalone'):
        gen = standalone_gen_for_chassis(None, assoc)
        if gen == aps_gen:
            return 1, 0
        return 0, 0
    mgmt = compute = 0
    for node, is_mgmt in _iter_assoc_nodes(assoc):
        gen = node_aps_generation(
            node.get('name', ''),
            node.get('role', ''),
            chassis_hostname=node.get('chassis_hostname', ''),
            fru_board_product=node.get('fru_board_product', ''),
            fru_product_name=node.get('fru_product_name', ''),
            aps_gen_hint=node.get('aps_gen', ''),
        )
        if gen != aps_gen:
            continue
        if is_mgmt:
            mgmt += 1
        else:
            compute += 1
    return mgmt, compute


def aps_generation_chip_stats(all_chassis_qs, node_by_id: dict[int, dict] | None = None):
    """Chip stats from node names: chassis with ≥1 node of that gen + mgmt/compute counts."""
    node_by_id = node_by_id or {}
    buckets: dict[str, dict] = {
        APS_GEN_10: {'chassis_ids': set(), 'mgmt': 0, 'compute': 0},
        APS_GEN_15: {'chassis_ids': set(), 'mgmt': 0, 'compute': 0},
    }
    for ch in all_chassis_qs:
        if ch.chassis_type not in KCOS_TYPES:
            continue
        assoc = node_by_id.get(ch.id)
        for gen in (APS_GEN_10, APS_GEN_15):
            mgmt, compute = count_nodes_for_aps_gen(assoc, gen)
            if mgmt or compute:
                buckets[gen]['chassis_ids'].add(ch.id)
                buckets[gen]['mgmt'] += mgmt
                buckets[gen]['compute'] += compute

    out = []
    for gen, label in APS_GEN_LABELS.items():
        b = buckets[gen]
        if not b['chassis_ids'] and not b['mgmt'] and not b['compute']:
            continue
        mgmt, compute = b['mgmt'], b['compute']
        out.append({
            'value': gen,
            'label': label,
            'chassis_count': len(b['chassis_ids']),
            'mgmt_count': mgmt,
            'compute_count': compute,
            'subtitle': f'{mgmt}+{compute}' if (mgmt or compute) else str(len(b['chassis_ids'])),
        })
    return out


def _count_compute_nodes_in_selection(
    chassis_list,
    node_by_id: dict,
    *,
    aps_gen: str | None = None,
) -> int:
    """Count compute nodes in multi-node chassis (excludes standalone mgmt+CN boxes)."""
    total = 0
    for ch in chassis_list:
        if ch.chassis_type in KCOS_TYPES and ch.chassis_type == 'aps_standalone':
            continue
        assoc = node_by_id.get(ch.id) or {}
        if assoc.get('standalone'):
            continue
        for cn in assoc.get('compute_nodes') or []:
            name = cn.get('name', '') or ''
            if aps_gen is None:
                if is_kcos_node_name(name):
                    total += 1
                continue
            if node_aps_generation(
                name, cn.get('role', ''), chassis_hostname=ch.hostname or '',
                fru_board_product=cn.get('fru_board_product', ''),
                aps_gen_hint=cn.get('aps_gen', ''),
            ) == aps_gen:
                total += 1
    return total


def _count_standalone_cn_in_selection(
    chassis_list,
    node_by_id: dict | None,
    *,
    aps_gen: str,
) -> int:
    """Count standalone mgmt+CN appliances by CN generation (1.0 / 1.5)."""
    node_by_id = node_by_id or {}
    total = 0
    for ch in chassis_list:
        if ch.chassis_type != 'aps_standalone':
            continue
        if standalone_gen_for_chassis(ch, node_by_id.get(ch.id)) == aps_gen:
            total += 1
    return total


def build_models_in_selection_breakdown(
    chassis_list,
    node_by_id: dict[int, dict] | None = None,
) -> list[dict]:
    """Chassis counts by model, then CN / standalone-CN counts by generation."""
    node_by_id = node_by_id or {}
    rows: list[dict] = []
    counter = Counter()
    for ch in chassis_list:
        counter[ch.get_chassis_type_display()] += 1
    for label, count in sorted(counter.items()):
        rows.append({'label': label, 'count': count, 'row_kind': 'chassis'})

    if any(ch.chassis_type in KCOS_TYPES for ch in chassis_list):
        cn15 = _count_compute_nodes_in_selection(
            chassis_list, node_by_id, aps_gen=APS_GEN_15,
        )
        cn10 = _count_compute_nodes_in_selection(
            chassis_list, node_by_id, aps_gen=APS_GEN_10,
        )
        sa15 = _count_standalone_cn_in_selection(
            chassis_list, node_by_id, aps_gen=APS_GEN_15,
        )
        sa10 = _count_standalone_cn_in_selection(
            chassis_list, node_by_id, aps_gen=APS_GEN_10,
        )
        if cn15:
            rows.append({
                'label': 'CN 1.5',
                'count': cn15,
                'row_kind': 'compute_node',
            })
        if cn10:
            rows.append({
                'label': 'CN 1.0',
                'count': cn10,
                'row_kind': 'compute_node',
            })
        if sa15:
            rows.append({
                'label': 'Standalone CN 1.5',
                'count': sa15,
                'row_kind': 'standalone_cn',
            })
        if sa10:
            rows.append({
                'label': 'Standalone CN 1.0',
                'count': sa10,
                'row_kind': 'standalone_cn',
            })
    return rows


def filtered_model_stats(chassis_list) -> list[dict]:
    """Count chassis by display type for the active filter set."""
    return build_models_in_selection_breakdown(chassis_list, None)


def chassis_aps_generation(chassis, node_by_id: dict[int, dict] | None = None) -> str | None:
    """Dominant APS generation for a chassis from its nodes (for badges)."""
    node_by_id = node_by_id or {}
    assoc = node_by_id.get(getattr(chassis, 'id', None))
    has_15 = chassis_has_aps_gen(assoc, APS_GEN_15)
    has_10 = chassis_has_aps_gen(assoc, APS_GEN_10)
    if has_15 and not has_10:
        return APS_GEN_15
    if has_10 and not has_15:
        return APS_GEN_10
    if has_15 and has_10:
        return 'mixed'
    return None
