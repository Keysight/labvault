"""Re-export opsd service catalog for Django CLI layer."""
from __future__ import annotations

import sys
from pathlib import Path

_opsd = Path(__file__).resolve().parents[2] / "opsd"
if str(_opsd) not in sys.path:
    sys.path.insert(0, str(_opsd))

from service_catalog import (  # noqa: E402
    ACTIONS,
    LOGICAL_NAMES,
    RESULT_STATES,
    SERVICES,
    capability_allows,
    restart_all_targets,
)

__all__ = [
    "ACTIONS",
    "LOGICAL_NAMES",
    "RESULT_STATES",
    "SERVICES",
    "capability_allows",
    "restart_all_targets",
]
