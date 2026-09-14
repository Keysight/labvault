from __future__ import annotations

import re
from typing import Any

SECRET_KEYS = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key|credential)",
    re.I,
)


def redact_payload(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if SECRET_KEYS.search(str(k)):
                out[k] = "***REDACTED***"
            else:
                out[k] = redact_payload(v)
        return out
    if isinstance(obj, list):
        return [redact_payload(x) for x in obj]
    if isinstance(obj, str):
        # scrub URL userinfo
        return re.sub(r"://[^/@\s]+:[^/@\s]+@", "://***:***@", obj)
    return obj
