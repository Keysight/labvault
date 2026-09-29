"""Django admin registration: every concrete ``connect`` model is auto-registered.

Access to ``/admin/`` itself is further limited to ``LABVAULT_DJANGO_ADMIN_USERNAMES``
by ``connect.django_admin_access.patch_django_admin_site_access`` (run from
``ConnectConfig.ready``). ``auth.User`` keeps Django's stock ``UserAdmin``; no custom
password-lock logic is applied in the admin.
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from . import models as m

# Auto-register retained models.
_SKIP = {"User"}  # auth.User handled below if customized

for _name, _cls in vars(m).items():
    if not isinstance(_cls, type):
        continue
    if not getattr(_cls, "_meta", None):
        continue
    if _cls._meta.abstract or _cls._meta.app_label != "connect":
        continue
    try:
        admin.site.register(_cls)
    except admin.sites.AlreadyRegistered:
        pass
