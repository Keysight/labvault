# `labvault/` — Django project package

The project shell around the single `connect` app. It holds the root URLconf and the WSGI
entry point. **Settings are not here**: `DJANGO_SETTINGS_MODULE` is `connect.settings`
(set by `manage.py` and `wsgi.py`).

| File | Purpose |
|---|---|
| `__init__.py` | Package marker (docstring only). |
| `urls.py` | Root URLconf (`ROOT_URLCONF`). Mounts `/admin/`, `/accounts/breakglass-reset-password/`, `/accounts/password_change/` (+ `done/`), a `/favicon.ico` redirect, static/media serving, then `include('connect.urls')` at `/`. |
| `wsgi.py` | `application` for gunicorn (`labvault.wsgi:application`, bound to `127.0.0.1:8000` behind nginx on 9443). |

## How it connects

```
nginx :9443 ──► gunicorn :8000 ──► labvault.wsgi.application
                                    └─► connect.settings (middleware, DBs, cache)
                                        └─► labvault.urls ──► connect.urls ──► view modules
```

- Password-change and break-glass views come from `connect/auth_views.py`.
- Admin access is restricted at startup by `connect/apps.py` → `connect/django_admin_access.py`.
- In non-DEBUG mode `/static/` falls back to `django.views.static.serve` from `STATIC_ROOT`;
  nginx normally serves it first.

Full description: [docs/development/subsystems/core-web.md](../docs/development/subsystems/core-web.md).
