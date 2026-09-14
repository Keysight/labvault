"""Shared GET-filter parsing for Keysight dashboard and hardware inventory."""

from __future__ import annotations

from collections import Counter
from urllib.parse import quote

from django.db import models as db_models

from .keysight_aps_generations import filter_chassis_queryset_by_aps_gen
from .models import KEYSIGHT_CHASSIS_TYPE_CHOICES, KeysightChassis


def keysight_dashboard_query_without(request, *omit_keys: str) -> str:
    """Current GET params as a query string, excluding omitted keys."""
    params = request.GET.copy()
    for k in omit_keys:
        params.pop(k, None)
    encoded = params.urlencode()
    return f'?{encoded}' if encoded else ''


def parse_keysight_filter_params(request) -> dict:
    team_tag_f = request.GET.get('team_tag', '')
    team_tags_selected = [t.strip() for t in team_tag_f.split(',') if t.strip()]
    return {
        'chassis_type': request.GET.get('chassis_type', '').strip(),
        'status': request.GET.get('status', '').strip(),
        'team_tag': team_tag_f,
        'team_tags_selected': team_tags_selected,
        'geo': request.GET.get('geo', '').strip(),
        'lab': request.GET.get('lab', '').strip(),
        'q': request.GET.get('q', '').strip(),
        'aps_gen': request.GET.get('aps_gen', '').strip(),
    }


def _keysight_dashboard_search_q(search_q: str):
    if not search_q:
        return None
    return (
        db_models.Q(hostname__icontains=search_q)
        | db_models.Q(ip_address__icontains=search_q)
        | db_models.Q(serial_number__icontains=search_q)
    )


def keysight_filtered_chassis_list(
    request,
    *,
    online_only: bool = False,
    node_by_id: dict | None = None,
    chassis_pool: list[KeysightChassis] | None = None,
) -> list[KeysightChassis]:
    """Return chassis matching dashboard-style filters (list, after team_tag OR filter)."""
    f = parse_keysight_filter_params(request)
    if chassis_pool is not None:
        chassis_list = list(chassis_pool)
        if online_only:
            chassis_list = [ch for ch in chassis_list if ch.status == 'online']
        if f['chassis_type']:
            chassis_list = [ch for ch in chassis_list if ch.chassis_type == f['chassis_type']]
        if f['status']:
            chassis_list = [ch for ch in chassis_list if ch.status == f['status']]
        if f['geo']:
            chassis_list = [ch for ch in chassis_list if ch.geo_location == f['geo']]
        if f['lab']:
            chassis_list = [ch for ch in chassis_list if ch.lab_name == f['lab']]
        if f['q']:
            q_lower = f['q'].lower()
            chassis_list = [
                ch for ch in chassis_list
                if q_lower in (ch.hostname or '').lower()
                or q_lower in (ch.ip_address or '').lower()
                or q_lower in (ch.serial_number or '').lower()
            ]
    else:
        qs = KeysightChassis.objects.all()
        if online_only:
            qs = qs.filter(status='online')
        if f['chassis_type']:
            qs = qs.filter(chassis_type=f['chassis_type'])
        if f['aps_gen']:
            qs = filter_chassis_queryset_by_aps_gen(qs, f['aps_gen'])
        if f['status']:
            qs = qs.filter(status=f['status'])
        if f['geo']:
            qs = qs.filter(geo_location=f['geo'])
        if f['lab']:
            qs = qs.filter(lab_name=f['lab'])
        sq = _keysight_dashboard_search_q(f['q'])
        if sq is not None:
            qs = qs.filter(sq)
        chassis_list = list(qs)
    if f['team_tags_selected']:
        chassis_list = [
            ch for ch in chassis_list
            if any(t in ch.team_tags_list for t in f['team_tags_selected'])
        ]
    if f['aps_gen']:
        from .keysight_aps_generations import filter_chassis_list_by_aps_gen
        from .keysight_node_associations import get_cached_node_associations

        if node_by_id is None:
            node_by_id = get_cached_node_associations([ch.id for ch in chassis_list])
        chassis_list = filter_chassis_list_by_aps_gen(
            chassis_list, f['aps_gen'], node_by_id,
        )
    return chassis_list


def build_keysight_filter_chip_context(
    request,
    all_chassis_qs,
    *,
    path: str | None = None,
    preserve_query: dict | None = None,
    all_chassis_list: list[KeysightChassis] | None = None,
) -> dict:
    """Chip stats + toggle URLs shared by dashboard and HW inventory."""
    f = parse_keysight_filter_params(request)
    path = path or request.path
    preserve_query = {k: v for k, v in (preserve_query or {}).items() if v}
    if all_chassis_list is None:
        all_chassis_list = (
            all_chassis_qs if isinstance(all_chassis_qs, list) else list(all_chassis_qs)
        )

    type_counter: Counter = Counter()
    status_counter: Counter = Counter()
    tag_counter: Counter = Counter()
    geo_counter: Counter = Counter()
    lab_counter: Counter = Counter()
    for ch in all_chassis_list:
        type_counter[ch.chassis_type] += 1
        status_counter[ch.status] += 1
        for t in ch.team_tags_list:
            tag_counter[t] += 1
        if ch.geo_location:
            geo_counter[ch.geo_location] += 1
        if ch.lab_name:
            lab_counter[ch.lab_name] += 1

    def _build_qparams(**overrides):
        parts = []
        ct = overrides.get('chassis_type', f['chassis_type'])
        ag = overrides.get('aps_gen', f['aps_gen'])
        st = overrides.get('status', f['status'])
        tt = overrides.get('team_tag', ','.join(f['team_tags_selected']) if f['team_tags_selected'] else '')
        ge = overrides.get('geo', f['geo'])
        la = overrides.get('lab', f['lab'])
        sq = overrides.get('q', f['q'])
        if ct:
            parts.append(f'chassis_type={ct}')
        if ag:
            parts.append(f'aps_gen={ag}')
        if st:
            parts.append(f'status={st}')
        if tt:
            parts.append(f'team_tag={tt}')
        if ge:
            parts.append(f'geo={quote(ge)}')
        if la:
            parts.append(f'lab={quote(la)}')
        if sq:
            parts.append(f'q={quote(sq)}')
        for pk, pv in preserve_query.items():
            if pk in overrides:
                continue
            parts.append(f'{pk}={quote(str(pv))}')
        return ('?' + '&'.join(parts)) if parts else path

    type_stats = []
    for val, label in KEYSIGHT_CHASSIS_TYPE_CHOICES:
        cnt = type_counter.get(val, 0)
        if cnt > 0:
            type_stats.append({
                'value': val, 'label': label, 'count': cnt,
                'is_selected': f['chassis_type'] == val,
                'toggle_url': _build_qparams(chassis_type=('' if f['chassis_type'] == val else val)),
            })

    status_stats = []
    for sv, sl in [('online', 'Online'), ('offline', 'Offline'), ('auth_failed', 'Auth Failed')]:
        cnt = status_counter.get(sv, 0)
        if cnt > 0:
            status_stats.append({
                'value': sv, 'label': sl, 'count': cnt,
                'is_selected': f['status'] == sv,
                'toggle_url': _build_qparams(status=('' if f['status'] == sv else sv)),
            })

    tag_stats = []
    for t, c in sorted(tag_counter.items()):
        if t in f['team_tags_selected']:
            new_selected = [x for x in f['team_tags_selected'] if x != t]
        else:
            new_selected = f['team_tags_selected'] + [t]
        tag_stats.append({
            'value': t, 'count': c,
            'is_selected': t in f['team_tags_selected'],
            'toggle_url': _build_qparams(team_tag=','.join(new_selected)),
        })

    geo_stats = [
        {'value': g, 'count': c, 'is_selected': f['geo'] == g,
         'toggle_url': _build_qparams(geo=('' if f['geo'] == g else g))}
        for g, c in sorted(geo_counter.items())
    ]

    lab_stats = [
        {'value': l, 'count': c, 'is_selected': f['lab'] == l,
         'toggle_url': _build_qparams(lab=('' if f['lab'] == l else l))}
        for l, c in sorted(lab_counter.items())
    ]

    filters_active = any([
        f['chassis_type'], f['aps_gen'], f['status'], f['team_tags_selected'],
        f['geo'], f['lab'], f['q'],
    ])

    return {
        **f,
        'chassis_type_filter': f['chassis_type'],
        'status_filter': f['status'],
        'team_tag_filter': f['team_tag'],
        'geo_filter': f['geo'],
        'lab_filter': f['lab'],
        'search_q': f['q'],
        'aps_gen_filter': f['aps_gen'],
        'chassis_type_choices': KEYSIGHT_CHASSIS_TYPE_CHOICES,
        'type_stats': type_stats,
        'status_stats': status_stats,
        'tag_stats': tag_stats,
        'geo_stats': geo_stats,
        'lab_stats': lab_stats,
        'dashboard_filters_active': filters_active,
        'build_qparams': _build_qparams,
        'clear_filter_url': path,
    }
