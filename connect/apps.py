from django.apps import AppConfig


class ConnectConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'connect'
    verbose_name = 'LabVault'

    def ready(self):
        from connect.django_admin_access import patch_django_admin_site_access
        from connect.np_timeseries_db import register_np_timeseries_pragmas

        patch_django_admin_site_access()
        register_np_timeseries_pragmas()
