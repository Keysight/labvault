"""
log_topology_action — write to TopologyAuditLog (Phase 6).
Call from any topology edit endpoint; failures are silently swallowed so they
never block the actual operation.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def log_topology_action(
    actor,
    action: str,
    *,
    topology=None,
    detail: str = '',
    extra: Any = None,
    request=None,
) -> None:
    """Create one TopologyAuditLog row; client IP comes from X-Forwarded-For or REMOTE_ADDR."""
    try:
        from .models import TopologyAuditLog

        ip = None
        if request:
            xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
            ip = xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR')

        TopologyAuditLog.objects.create(
            topology=topology,
            actor=actor if actor and actor.is_authenticated else None,
            action=action,
            detail=detail[:2000],
            extra_json=extra,
            ip_address=ip or None,
        )
    except Exception as exc:
        logger.debug('topology_audit: %s', exc)
