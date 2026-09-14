"""Report Capex/topology migration drift and optional repair hints for production ops."""
from django.core.management.base import BaseCommand
from django.db import connection


def _sqlite_table_exists(cursor, table):
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        [table],
    )
    return cursor.fetchone() is not None


def _sqlite_columns(cursor, table):
    cursor.execute(f'PRAGMA table_info("{table}")')
    return {row[1] for row in cursor.fetchall()}


class Command(BaseCommand):
    help = (
        'Check common migration drift (0038 node_key, 0026 UserProfile, 0051/0052 prefs). '
        'Exit 0 when healthy, 1 when action is recommended.'
    )

    def handle(self, *args, **options):
        issues = []
        with connection.cursor() as cursor:
            if connection.vendor != 'sqlite':
                self.stdout.write(self.style.WARNING(
                    f'DB vendor is {connection.vendor}; checks tuned for SQLite production.'
                ))

            cursor.execute(
                "SELECT name FROM django_migrations WHERE app='connect' ORDER BY name"
            )
            applied = {row[0] for row in cursor.fetchall()}

            # 0038 / node_key
            if '0038_labtopologynode_node_key_default' in applied:
                if _sqlite_table_exists(cursor, 'connect_labtopologynode'):
                    cols = _sqlite_columns(cursor, 'connect_labtopologynode')
                    if 'node_key' not in cols:
                        issues.append(
                            '0038 is applied but connect_labtopologynode.node_key is missing. '
                            'Run: python manage.py migrate connect 0053'
                        )
                else:
                    issues.append('connect_labtopologynode table missing (topology not migrated).')

            # 0026 vs 0034 UserProfile lifecycle
            if '0026_userprofile_capex_defaults' in applied:
                has_profile = _sqlite_table_exists(cursor, 'connect_userprofile')
                dropped = '0034_lab_topology' in applied
                if has_profile and dropped:
                    issues.append(
                        'connect_userprofile still exists after 0034_lab_topology '
                        '(harmless drift; Capex defaults use LabvaultUserPrefs). '
                        'After backup, optional: DROP TABLE connect_userprofile; if unused.'
                    )
                if not has_profile and not dropped:
                    issues.append(
                        '0026 applied but connect_userprofile missing and 0034 not applied. '
                        'Run migrate or fake 0034 after backup if intentional.'
                    )

            for mig, table in (
                ('0051_labvault_user_prefs', 'connect_labvaultuserprefs'),
                ('0052_labvault_global_prefs', 'connect_labvaultglobalprefs'),
            ):
                if mig in applied and not _sqlite_table_exists(cursor, table):
                    issues.append(f'{mig} applied but {table} missing. Re-run migrate.')

        if not issues:
            self.stdout.write(self.style.SUCCESS('Migration health: OK'))
            return

        self.stdout.write(self.style.ERROR('Migration health issues:'))
        for item in issues:
            self.stdout.write(f'  - {item}')
        self.stdout.write(
            '\nRecommended on production:\n'
            '  1. Backup db.sqlite3\n'
            '  2. python manage.py migrate --noinput\n'
            '  3. python manage.py labvault_migration_health\n'
            '  4. systemctl restart labvault.service labvault-poller.service\n'
        )
        raise SystemExit(1)
