"""App config for ``connect`` — the single Django app that implements LabVault."""
from django.apps import AppConfig


class ConnectConfig(AppConfig):
    """Registers process-wide hooks once the app registry is ready."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'connect'
    verbose_name = 'LabVault'

    def ready(self):
        """Restrict Django ``/admin/`` to break-glass usernames and enable np_timeseries SQLite pragmas.

        Both hooks are idempotent; no background threads are started here.
        """
        from connect.django_admin_access import patch_django_admin_site_access
        from connect.np_timeseries_db import register_np_timeseries_pragmas

        patch_django_admin_site_access()
        register_np_timeseries_pragmas()
