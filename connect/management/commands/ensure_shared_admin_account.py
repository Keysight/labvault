"""Keep shared ``admin`` login working for main LabVault UI (not Django /admin/)."""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from connect.password_policy import set_user_password


class Command(BaseCommand):
    help = (
        'Ensure shared user "admin" can log in at /login/ (active superuser, is_staff=False). '
        'Set password from LABVAULT_SHARED_ADMIN_PASSWORD or --password. '
        'Never disables the account or clears the password unless --set-password is passed.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            default='admin',
            help='Shared UI account (default: admin).',
        )
        parser.add_argument(
            '--password',
            default='',
            help='Password to set (overrides LABVAULT_SHARED_ADMIN_PASSWORD).',
        )
        parser.add_argument(
            '--set-password',
            action='store_true',
            help='Apply password from --password or env; required to fix unusable passwords.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print actions without saving.',
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = (options['username'] or 'admin').strip()
        dry_run = options['dry_run']
        set_password = options['set_password']
        password = (options['password'] or '').strip()
        if set_password and not password:
            password = os.environ.get('LABVAULT_SHARED_ADMIN_PASSWORD', '').strip()
        if set_password and not password:
            raise CommandError(
                'Pass --password or set LABVAULT_SHARED_ADMIN_PASSWORD in the environment.'
            )

        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                'is_active': True,
                'is_staff': False,
                'is_superuser': True,
                'email': f'{username}@labvault.local',
            },
        )
        changes = []
        if not user.is_active:
            changes.append('is_active=True')
        if user.is_staff:
            changes.append('is_staff=False')
        if not user.is_superuser:
            changes.append('is_superuser=True')
        if set_password:
            changes.append('password=***')

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f'[dry-run] {username!r}: '
                    + (', '.join(changes) if changes else 'no flag changes')
                )
            )
            return

        user.is_active = True
        user.is_staff = False
        user.is_superuser = True
        if set_password:
            set_user_password(user, password)
        else:
            user.save(update_fields=['is_active', 'is_staff', 'is_superuser'])

        verb = 'Created' if created else 'Updated'
        self.stdout.write(
            self.style.SUCCESS(
                f'{verb} {username!r} for main UI (/login/); Django /admin/ remains blocked.'
            )
        )
        if not set_password and not user.has_usable_password():
            self.stdout.write(
                self.style.ERROR(
                    f'{username!r} still has no usable password. Run with --set-password and '
                    'LABVAULT_SHARED_ADMIN_PASSWORD (team shared password).'
                )
            )
