"""Extract client IP, user-agent, and parsed browser/OS from Django requests.

Overlaps with ``connect.request_audit``; this variant also resolves a PTR hostname.
No module in this tree currently imports it.
"""
from __future__ import annotations

import re
import socket
from typing import Any


def _client_ip_from_meta(meta: dict) -> str:
    xff = meta.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return (meta.get('REMOTE_ADDR') or '').strip()


def ptr_lookup(ip: str) -> str:
    """Reverse-DNS ``ip`` (blocking, ~0.35 s cap); ``''`` for loopback or on failure.

    ``socket.setdefaulttimeout`` is process-global, so concurrent threads briefly
    inherit the short timeout while this runs.
    """
    if not ip or ip in ('127.0.0.1', '::1'):
        return ''
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(0.35)
        host, _, _ = socket.gethostbyaddr(ip)
        return (host or '')[:255]
    except Exception:
        return ''
    finally:
        try:
            socket.setdefaulttimeout(old)
        except Exception:
            pass


def parse_user_agent(ua: str) -> dict[str, str]:
    """Lightweight UA parse without external dependencies."""
    ua = (ua or '').strip()
    if not ua:
        return {'browser': '', 'os': ''}
    os_name = ''
    if re.search(r'Windows NT', ua, re.I):
        os_name = 'Windows'
    elif re.search(r'Mac OS X|Macintosh', ua, re.I):
        os_name = 'macOS'
    elif re.search(r'Linux|X11', ua, re.I):
        os_name = 'Linux'
    elif re.search(r'Android', ua, re.I):
        os_name = 'Android'
    elif re.search(r'iPhone|iPad', ua, re.I):
        os_name = 'iOS'

    browser = ''
    if re.search(r'Edg/', ua):
        browser = 'Edge'
    elif re.search(r'Chrome/', ua) and not re.search(r'Edg/', ua):
        browser = 'Chrome'
    elif re.search(r'Firefox/', ua):
        browser = 'Firefox'
    elif re.search(r'Safari/', ua) and not re.search(r'Chrome/', ua):
        browser = 'Safari'
    elif re.search(r'curl/', ua, re.I):
        browser = 'curl'
    elif re.search(r'python-requests', ua, re.I):
        browser = 'Python requests'

    return {'browser': browser[:64], 'os': os_name[:64]}


def request_context(request) -> dict[str, Any]:
    """Actor + network context for audit / change-log rows."""
    user = None
    username = ''
    if request and getattr(request, 'user', None) and request.user.is_authenticated:
        user = request.user
        username = user.get_username()
    meta = getattr(request, 'META', {}) if request else {}
    ip = _client_ip_from_meta(meta)
    ua = (meta.get('HTTP_USER_AGENT') or '')[:500]
    parsed = parse_user_agent(ua)
    return {
        'user': user,
        'username': username,
        'ip': ip,
        'hostname': ptr_lookup(ip),
        'user_agent': ua,
        'browser': parsed['browser'],
        'os': parsed['os'],
        'path': (getattr(request, 'path', None) or '')[:500] if request else '',
    }
