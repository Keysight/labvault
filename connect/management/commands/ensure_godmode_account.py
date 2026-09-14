"""Provision break-glass superuser godmode (reset admin / mgmt / finance via Django admin)."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Ensure superuser godmode exists (default password: godmode). '
        'Only godmode may log in to Django /admin/ (shared "admin" is deprecated). '
        'Reset mgmt/finance passwords via break-glass UI or godmode in /admin/. '
        'Rotate godmode with: manage.py changepassword godmode'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--password',
            default='godmode',
            help='Initial password if the user is created or you want to reset it (default: godmode).',
        )
        parser.add_argument(
            '--reset-password',
            action='store_true',
            help='Always set password from --password (use after lockout).',
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = 'godmode'
        password = options['password'] or 'godmode'
        reset = options['reset_password']

        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                'is_superuser': True,
                'is_staff': True,
                'email': 'godmode@labvault.local',
            },
        )
        user.is_superuser = True
        user.is_staff = True
        if not user.email:
            user.email = 'godmode@labvault.local'
        if created or reset:
            user.set_password(password)
        user.save()

        verb = 'Created' if created else ('Reset password for' if reset else 'Updated')
        self.stdout.write(
            self.style.SUCCESS(
                f'{verb} {username!r} — staff/superuser; sole /admin/ login. '
                'Use break-glass UI or /admin/ Users to reset mgmt/finance when locked.'
            )
        )
        if created or reset:
            self.stdout.write(
                self.style.WARNING(
                    'Change the godmode password in production (changepassword or admin) '
                    'and restrict who can run this command.'
                )
            )
