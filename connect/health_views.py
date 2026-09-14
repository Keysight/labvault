"""Unauthenticated liveness/readiness endpoints (customer SKU)."""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def health_live(request):
    """Process is up — no auth, no version/host/secret detail."""
    return JsonResponse({"status": "live"})


@require_GET
def health_ready(request):
    """Ready when DBs, migrations, and cache are usable. Never probes lab hardware."""
    errors: list[str] = []
    for alias in ("default", "np_timeseries"):
        try:
            connections[alias].ensure_connection()
        except Exception:  # noqa: BLE001
            errors.append(f"db_{alias}_unreachable")
    try:
        cache.set("labvault_ready_probe", "1", 5)
        if cache.get("labvault_ready_probe") != "1":
            errors.append("cache_unwritable")
    except Exception:  # noqa: BLE001
        errors.append("cache_error")
    # Static manifest optional in DEV; require when not DEBUG
    if not settings.DEBUG:
        static_root = getattr(settings, "STATIC_ROOT", None)
        if static_root:
            from pathlib import Path
            root = Path(static_root)
            # Require collected static only when the directory exists but is empty after an install attempt.
            if root.exists() and not any(root.iterdir()):
                errors.append("static_empty")
    try:
        from django.db.migrations.executor import MigrationExecutor

        for alias in ("default", "np_timeseries"):
            if alias not in connections:
                continue
            executor = MigrationExecutor(connections[alias])
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
            if plan:
                errors.append(f"migrations_pending_{alias}")
    except Exception:  # noqa: BLE001
        errors.append("migrations_check_failed")
    if errors:
        return JsonResponse({"status": "not_ready", "errors": errors}, status=503)
    return JsonResponse({"status": "ready"})
