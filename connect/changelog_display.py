"""UI helpers for reachability badges on Keysight dashboard."""
from __future__ import annotations

from django.utils import timezone


def chassis_reachability_badge(chassis_id: int, status: str) -> dict:
    """
    Return {label, css_class} for extended/temporary down badges, or empty label.
    """
    if status == 'online':
        return {'label': '', 'css_class': ''}
    try:
        from .models import DeviceStateBaseline
        bl = DeviceStateBaseline.objects.filter(
            target_kind='chassis', target_id=chassis_id,
        ).first()
    except Exception:
        bl = None
    if not bl or not bl.last_offline_at:
        return {'label': '', 'css_class': ''}
    days = (timezone.now() - bl.last_offline_at).total_seconds() / 86400.0
    if bl.last_emitted_state == 'extended_down' or days >= 3:
        return {'label': f'Down ({int(days)}d)', 'css_class': 'badge bg-danger'}
    if bl.last_emitted_state == 'temporarily_down' or days >= 1:
        return {'label': 'Temp Down', 'css_class': 'badge bg-warning text-dark'}
    return {'label': '', 'css_class': ''}
