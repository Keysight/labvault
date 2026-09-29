"""Root URLconf (``ROOT_URLCONF = 'labvault.urls'``).

Mounts, in order: Django ``/admin/`` (restricted by ``connect.django_admin_access``),
the break-glass password reset and password-change views from ``connect.auth_views``,
a ``/favicon.ico`` redirect, static/media serving, and finally ``connect.urls`` at ``/``.

``connect.urls`` is included last, so project-level routes here take precedence over
any same-path route in the app. In non-DEBUG mode ``/static/`` is served by ``django.views.static.serve`` from
``STATIC_ROOT`` as a fallback when nginx does not serve it directly.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.generic import RedirectView
from django.views.static import serve

from connect.auth_views import (
    LabvaultPasswordChangeView,
    breakglass_reset_user_password,
)


urlpatterns = [
    path('admin/', admin.site.urls),
    path(
        'accounts/breakglass-reset-password/',
        breakglass_reset_user_password,
        name='breakglass_reset_user_password',
    ),
    path(
        'accounts/password_change/',
        LabvaultPasswordChangeView.as_view(),
        name='password_change',
    ),
    path(
        'accounts/password_change/done/',
        auth_views.PasswordChangeDoneView.as_view(
            template_name='connect/auth/password_change_done.html',
        ),
        name='password_change_done',
    ),
    path(
        'favicon.ico',
        RedirectView.as_view(
            url=f'{settings.STATIC_URL}branding/mark-light-favicon.png',
            permanent=False,
        ),
    ),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
else:
    urlpatterns += [
        re_path(
            r'^static/(?P<path>.*)$',
            serve,
            {'document_root': settings.STATIC_ROOT},
        ),
    ]

urlpatterns += [
    path('', include('connect.urls')),
]
