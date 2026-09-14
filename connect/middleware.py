"""
Request logging middleware for LabVault.

Logs every authenticated HTTP request to the RequestLog table so that
the access-by-IP audit page can show full page-visit history, not just
explicit user actions.

Excluded to avoid noise:
  - Unauthenticated requests (not logged in)
  - Static / media file requests (/static/, /media/)
  - High-frequency polling endpoints (live-status, keysight dashboard API, deploy status)
  - AJAX chassis data endpoints that fire every ~10 s automatically
"""

import threading

# Simple in-process write queue so the middleware never blocks the response.
_queue = []
_queue_lock = threading.Lock()
_flush_timer = None


# Paths that fire automatically in the background — skip them to avoid log spam.
_EXCLUDED_PREFIXES = (
    '/static/',
    '/media/',
)

_EXCLUDED_PATHS = {
    '/api/live-status/',
    '/keysight/api/dashboard/',
    '/keysight/api/deploy/status/',
    '/favicon.ico',
}

_EXCLUDED_PATH_PREFIXES = (
    '/keysight/api/chassis/',   # per-chassis polling
)


def _should_skip(path):
    if any(path.startswith(p) for p in _EXCLUDED_PREFIXES):
        return True
    if path in _EXCLUDED_PATHS:
        return True
    if any(path.startswith(p) for p in _EXCLUDED_PATH_PREFIXES):
        return True
    return False


def _get_client_ip(request):
    x_fwd = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_fwd:
        return x_fwd.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _flush_queue():
    """Write queued RequestLog entries to DB in one batch."""
    global _flush_timer
    with _queue_lock:
        items = _queue[:]
        _queue.clear()
        _flush_timer = None

    if not items:
        return

    try:
        from connect.models import RequestLog
        RequestLog.objects.bulk_create(items, ignore_conflicts=True)
    except Exception:
        pass  # Never crash the app due to logging


class RequestLogMiddleware:
    """WSGI-compatible middleware that logs every authenticated request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        try:
            # Only log authenticated requests
            user = getattr(request, 'user', None)
            if user is None or not user.is_authenticated:
                return response

            path = request.path
            if _should_skip(path):
                return response

            from connect.models import RequestLog
            entry = RequestLog(
                user=user,
                ip_address=_get_client_ip(request),
                method=request.method,
                path=path,
                status_code=response.status_code,
            )

            # Queue and flush asynchronously to avoid blocking
            global _flush_timer
            with _queue_lock:
                _queue.append(entry)
                if _flush_timer is None:
                    import threading as _threading
                    _flush_timer = _threading.Timer(2.0, _flush_queue)
                    _flush_timer.daemon = True
                    _flush_timer.start()
        except Exception:
            pass  # Never let logging crash the request

        return response
