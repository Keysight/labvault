"""
IxOS/KCOS card_type → stable family tokens (CS, PS, APS, M8400, ARESONE, XGS…)
for matching requested card families against chassis inventory.
"""
from __future__ import annotations

import re
from typing import Optional

# (family_token, substring hints in normalized card_type)
_CARD_FAMILY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('CS', ('cloudstorm', 'cs100', 'cs400', 'cs800', 'cs25', 'cs10', 'cs50', 'cs200')),
    ('PS', ('perfectstorm', 'ps100', 'ps400', 'ps25', 'ps10', 'ps50', 'ps200')),
    ('APS', ('aps', 'm8040', 'application', 'apsn')),
    ('M8400', ('m8400', '8400')),
    ('ARESONE', ('aresone', 'ares one', '800ge', '400gbase', 'm800')),
    ('XGS12', ('xgs12', 'xgs-12')),
    ('XGS2', ('xgs2', 'xgs-2', 'xgs02')),
)


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


def normalize_card_family(ixos_type: str) -> str:
    """Map raw IxOS card model string to a short family token (or '')."""
    raw = _norm(ixos_type)
    if not raw:
        return ''
    for family, hints in _CARD_FAMILY_RULES:
        if any(h in raw for h in hints):
            return family
    if raw.startswith('cs'):
        return 'CS'
    if raw.startswith('ps'):
        return 'PS'
    return ''


def card_family_matches(requested: str, ixos_type: str) -> bool:
    """True when requested family token matches ixos_type (exact family or substring)."""
    req = (requested or '').strip().upper()
    if not req:
        return True
    raw = (ixos_type or '').strip()
    if not raw:
        return False
    fam = normalize_card_family(raw)
    if fam and fam == req:
        return True
    if _norm(req) in _norm(raw):
        return True
    if req in ('CLOUDSTORM',) and fam == 'CS':
        return True
    if req in ('PERFECTSTORM',) and fam == 'PS':
        return True
    return False


def _coerce_str(val) -> str:
    if val is None:
        return ''
    if isinstance(val, str):
        return val
    return str(val)


def infer_line_rate(ixos_type: str, speed: str = '') -> str:
    """Best-effort line rate label from card type or port speed."""
    sp = _coerce_str(speed).strip().upper()
    if sp.isdigit():
        mbps = int(sp)
        if mbps >= 1000:
            g = mbps // 1000
            if g >= 1000:
                return f'{g // 1000}T' if g % 1000 == 0 else f'{g}G'
            return f'{g}G'
    if sp:
        return sp
    raw = _coerce_str(ixos_type).upper()
    for token in ('800G', '400G', '200G', '100G', '50G', '25G', '10G'):
        if token.replace('G', '') in raw.replace('GE', 'G') or token in raw:
            return token
    m = re.search(r'(\d+)G', raw)
    if m:
        return f'{m.group(1)}G'
    return ''
