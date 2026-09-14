# HARD_DUMP_REMOVED — customer stub/compat
"""Customer SKU: demo mutate path is hard-disabled (always off)."""


def demo_mode_enabled() -> bool:
    return False


def mutation_blocked_response(request=None, *args, **kwargs):
    return None
