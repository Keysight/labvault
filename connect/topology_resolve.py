"""Resolve a LabTopology by unique name. No LaaS / B2B dependency."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def resolve_topology_by_labname(
    labname: str,
    *,
    topo_id_hint: Optional[int] = None,
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Return (LabTopology, None) or (None, error_dict with suggested HTTP status)."""
    from .models import LabTopology

    name = (labname or '').strip()
    if topo_id_hint is None and not name:
        return None, {
            'error': 'labname_required',
            'detail': 'labname or labvault_topology_id required',
            'status': 400,
        }

    matches = []
    if name:
        matches = list(
            LabTopology.objects.filter(name=name).order_by('-updated_at').values(
                'id', 'name', 'updated_at',
            )
        )
    if topo_id_hint is not None and topo_id_hint != '':
        try:
            topo = LabTopology.objects.get(pk=int(topo_id_hint))
        except (LabTopology.DoesNotExist, ValueError, TypeError):
            return None, {'error': 'topology_not_found', 'topology_id': topo_id_hint, 'status': 404}
        if name and topo.name != name:
            return None, {
                'error': 'labname_topology_mismatch',
                'detail': f'labname={name!r} does not match topology id={topo_id_hint} name={topo.name!r}',
                'status': 400,
            }
        return topo, None

    if not matches:
        return None, {'error': 'topology_not_found', 'labname': name, 'status': 404}
    if len(matches) > 1:
        return None, {
            'error': 'topology_name_ambiguous',
            'labname': name,
            'matches': [
                {
                    'id': m['id'],
                    'name': m['name'],
                    'updated_at': m['updated_at'].isoformat() if m.get('updated_at') else None,
                }
                for m in matches
            ],
            'status': 409,
        }
    return LabTopology.objects.get(pk=matches[0]['id']), None
