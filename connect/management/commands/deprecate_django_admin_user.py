"""Revoke Django ``/admin/`` for shared accounts; keep main LabVault login working."""
import os

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand

from connect.django_admin_access import django_admin_allowed_usernames


class Command(BaseCommand):
    help = (
        'Remove Django /admin/ access for the shared "admin" user (is_staff=False) while '
        'keeping the account active for the main LabVault UI (/login/). '
        'Only LABVAULT_DJANGO_ADMIN_USERNAMES (default: godmode) may use /admin/.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            default='admin',
            help='Username to revoke from Django /admin/ (default: admin).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print planned changes without saving.',
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = (options['username'] or 'admin').strip()
        dry_run = options['dry_run']
        allowed = django_admin_allowed_usernames()

        self.stdout.write(
            self.style.NOTICE(
                'Django /admin/ allowed usernames: '
                + ', '.join(sorted(allowed))
            )
        )

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            self.stdout.write(
                self.style.WARNING(f'User {username!r} does not exist; nothing to update.')
            )
        else:
            changes = []
            if not user.is_active:
                changes.append('is_active=True')
            if user.is_staff:
                changes.append('is_staff=False')
            if not user.is_superuser:
                changes.append('is_superuser=True')
            if changes:
                msg = f'Revoke /admin/ for {username!r} (main UI unchanged): ' + ', '.join(changes)
                if dry_run:
                    self.stdout.write(self.style.WARNING(f'[dry-run] {msg}'))
                else:
                    user.is_active = True
                    user.is_staff = False
                    user.is_superuser = True
                    user.save(update_fields=['is_active', 'is_staff', 'is_superuser'])
                    self.stdout.write(self.style.SUCCESS(msg))
                    if not user.has_usable_password():
                        self.stdout.write(
                            self.style.WARNING(
                                f'{username!r} has no usable password — log in as godmode and set it via '
                                '/accounts/breakglass-reset-password/ or manage.py changepassword '
                                f'{username}'
                            )
                        )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'User {username!r} already has main-app access and no Django /admin/ staff flag.'
                    )
                )

        if not dry_run:
            call_command('ensure_godmode_account')
            pw_env = os.environ.get('LABVAULT_SHARED_ADMIN_PASSWORD', '').strip()
            if pw_env:
                call_command(
                    'ensure_shared_admin_account',
                    '--set-password',
                    password=pw_env,
                )
            else:
                call_command('ensure_shared_admin_account')
        else:
            self.stdout.write(
                self.style.WARNING('[dry-run] Would run ensure_godmode_account')
            )

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry run complete; no database changes saved.'))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    'Done. Main LabVault: /login/ as admin (and other accounts). '
                    'Django /admin/: godmode only. Rotate godmode: manage.py changepassword godmode'
                )
            )
