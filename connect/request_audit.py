"""HTTP request metadata for audit and change-log attribution."""
from __future__ import annotations

import re
from typing import Any

from django.http import HttpRequest


def client_ip(request: HttpRequest | None) -> str | None:
    if not request:
        return None
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        ip = xff.split(',')[0].strip()
        return ip or None
    return request.META.get('REMOTE_ADDR') or None


def client_user_agent(request: HttpRequest | None, *, max_len: int = 500) -> str:
    if not request:
        return ''
    return (request.META.get('HTTP_USER_AGENT') or '')[:max_len]


def parse_user_agent(ua: str) -> dict[str, str]:
    """Lightweight UA parse (no external deps)."""
    ua = (ua or '').strip()
    if not ua:
        return {'browser': '', 'os': '', 'device': ''}

    browser = 'Other'
    if 'Edg/' in ua or 'Edge/' in ua:
        browser = 'Edge'
    elif 'Chrome/' in ua and 'Chromium' not in ua:
        browser = 'Chrome'
    elif 'Firefox/' in ua:
        browser = 'Firefox'
    elif 'Safari/' in ua and 'Chrome' not in ua:
        browser = 'Safari'

    os_name = 'Other'
    if 'Windows NT' in ua:
        os_name = 'Windows'
    elif 'Mac OS X' in ua or 'Macintosh' in ua:
        os_name = 'macOS'
    elif 'Linux' in ua and 'Android' not in ua:
        os_name = 'Linux'
    elif 'Android' in ua:
        os_name = 'Android'
    elif 'iPhone' in ua or 'iPad' in ua:
        os_name = 'iOS'

    device = 'Desktop'
    if 'Mobile' in ua or 'Android' in ua or 'iPhone' in ua:
        device = 'Mobile'
    elif 'iPad' in ua or 'Tablet' in ua:
        device = 'Tablet'

    return {'browser': browser, 'os': os_name, 'device': device}


def request_actor_context(request: HttpRequest | None) -> dict[str, Any]:
    """Username, IP, UA, and parsed browser/OS for change-log rows."""
    username = ''
    if request and getattr(request, 'user', None) and request.user.is_authenticated:
        username = request.user.get_username() or ''
    ua = client_user_agent(request)
    parsed = parse_user_agent(ua)
    return {
        'username': username,
        'ip_address': client_ip(request),
        'user_agent': ua,
        'browser': parsed['browser'],
        'os': parsed['os'],
        'device': parsed['device'],
    }


def actor_from_body(body: dict | None, request: HttpRequest | None) -> dict[str, Any]:
    """Merge request context with optional script-supplied actor fields."""
    ctx = request_actor_context(request)
    if not body:
        return ctx
    if body.get('actor_username'):
        ctx['username'] = str(body['actor_username'])[:150]
    if body.get('ip_address'):
        ctx['ip_address'] = str(body['ip_address'])[:45]
    if body.get('user_agent'):
        ua = str(body['user_agent'])[:500]
        ctx['user_agent'] = ua
        parsed = parse_user_agent(ua)
        ctx['browser'] = parsed['browser']
        ctx['os'] = parsed['os']
        ctx['device'] = parsed['device']
    return ctx


_VERSION_PART = re.compile(r'\d+|\D+')


def version_key(version: str) -> tuple:
    """Sortable key for dotted build strings (9.25.1, 2024.03, etc.)."""
    version = (version or '').strip()
    if not version:
        return ()
    parts: list = []
    for chunk in _VERSION_PART.findall(version):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            parts.append(chunk.lower())
    return tuple(parts)


def classify_version_change(old_version: str, new_version: str) -> str:
    """Return upgrade, downgrade, or deploy_start when direction is unclear."""
    old_v = (old_version or '').strip()
    new_v = (new_version or '').strip()
    if not new_v:
        return 'deploy_start'
    if not old_v:
        return 'upgrade'
    ok, nk = version_key(old_v), version_key(new_v)
    if nk > ok:
        return 'upgrade'
    if nk < ok:
        return 'downgrade'
    return 'deploy_start'
