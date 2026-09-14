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
    env_mode = (getattr(settings, "WORKER_MODE_ENV", None) or "").strip().lower()
    if key in ("collector_mode", "heartbeat_mode") and env_mode in ("idle", "live"):
        return env_mode
    default = SCHEMA.get(key, {}).get("default")
    try:
        row = RuntimeSetting.objects.filter(key=key).first()
    except Exception:  # noqa: BLE001
        return getattr(settings, "LABVAULT_WORKER_DEFAULT_MODE", default)
    if not row:
        return getattr(settings, "LABVAULT_WORKER_DEFAULT_MODE", default) or default
    val = row.value_json
    if isinstance(val, dict) and "value" in val:
        return val["value"]
    return val


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
