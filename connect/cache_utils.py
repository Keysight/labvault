"""Safe wrappers around Django file cache (tolerate permission errors from mixed root/labvault writes)."""

from __future__ import annotations

import logging

from django.core.cache import cache as _django_cache

logger = logging.getLogger(__name__)


def cache_get(key, default=None):
    try:
        return _django_cache.get(key, default)
    except (PermissionError, OSError) as exc:
        logger.warning('cache get failed for %r: %s', key, exc)
        return default


def cache_set(key, value, timeout=None) -> bool:
    try:
        _django_cache.set(key, value, timeout)
        return True
    except (PermissionError, OSError) as exc:
        logger.warning('cache set failed for %r: %s', key, exc)
        return False


def cache_delete(key) -> None:
    try:
        _django_cache.delete(key)
    except (PermissionError, OSError) as exc:
        logger.warning('cache delete failed for %r: %s', key, exc)
