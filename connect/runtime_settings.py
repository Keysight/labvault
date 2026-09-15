"""Typed runtime settings helpers (customer SKU)."""
from __future__ import annotations

from typing import Any

from django.conf import settings

from connect.models import RuntimeSetting

SCHEMA: dict[str, dict[str, Any]] = {
    "collector_mode": {"type": "enum", "choices": ["idle", "live"], "default": "idle"},
    "heartbeat_mode": {"type": "enum", "choices": ["idle", "live"], "default": "idle"},
}


def get_setting(key: str) -> Any:
    """Prefer a stored RuntimeSetting so a dataset import can turn Pulse live

    even when compose still has ``LABVAULT_WORKER_MODE=idle``. Env is the
    fallback when no row exists (empty oneshot stays idle).
    """
    default = SCHEMA.get(key, {}).get("default")
    try:
        row = RuntimeSetting.objects.filter(key=key).first()
    except Exception:  # noqa: BLE001
        row = None
    if row:
        val = row.value_json
        if isinstance(val, dict) and "value" in val:
            val = val["value"]
        if key in ("collector_mode", "heartbeat_mode"):
            mode = str(val or "").strip().lower()
            if mode in ("idle", "live"):
                return mode
        else:
            return val
    env_mode = (getattr(settings, "WORKER_MODE_ENV", None) or "").strip().lower()
    if key in ("collector_mode", "heartbeat_mode") and env_mode in ("idle", "live"):
        return env_mode
    return getattr(settings, "LABVAULT_WORKER_DEFAULT_MODE", default) or default


def set_setting(key: str, value: Any, *, user=None) -> RuntimeSetting:
    if key not in SCHEMA:
        raise KeyError(key)
    choices = SCHEMA[key].get("choices")
    if choices and value not in choices:
        raise ValueError(f"{key} must be one of {choices}")
    row, _ = RuntimeSetting.objects.get_or_create(
        key=key, defaults={"value_json": {"value": value}}
    )
    row.value_json = {"value": value}
    row.version = int(row.version) + 1
    row.updated_by = user
    row.save(update_fields=["value_json", "version", "updated_by", "updated_at"])
    return row
