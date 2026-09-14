"""Customer SKU feature surface (baked defaults — no DISABLE_* laundry list)."""
import os


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Customer v1: excluded domains are hard-dumped (not flag-gated).
# Retained optional toggles are limited to runtime worker mode helpers.

# Collector / heartbeat mode is owned by RuntimeSetting; env is emergency override only.
WORKER_MODE_ENV = os.environ.get("LABVAULT_WORKER_MODE", "").strip().lower()  # idle|live|""

# Hard-dumped domains — keep symbols so leftover guards fail closed.
AI_NEXUS_ENABLED = False
