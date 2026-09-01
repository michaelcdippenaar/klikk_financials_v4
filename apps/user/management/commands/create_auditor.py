"""
Management command to create (or convert) a read-only auditor account.

Auditor accounts are hard-gated by AuditorGateMiddleware to safe-method
requests on /audit/ only. Give each external auditor their OWN account so
the access trail says who looked at what — never a shared login.

Usage:
    python manage.py create_auditor <username> --email a@firm.co.za
        Creates the user with role=auditor and prints a generated password.

    python manage.py create_auditor <existing-username> --convert
        Sets role=auditor on an existing account (e.g. to downgrade one).
"""
import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

User = get_user_model()


class Command(BaseCommand):
    help = 'Create a read-only auditor account (role=auditor)'

    def add_arguments(self, parser):
        parser.add_argument('username', type=str)
        parser.add_argument('--email', type=str, default='')
        parser.add_argument('--first-name', type=str, default='')
        parser.add_argument('--last-name', type=str, default='')
        parser.add_argument(
            '--convert',
            action='store_true',
            help='Convert an existing user to role=auditor instead of creating one',
        )

    def handle(self, *args, **options):
        username = options['username']
        existing = User.objects.filter(username=username).first()

        if options['convert']:
            if existing is None:
                raise CommandError(f'No user named {username!r} to convert')
            existing.role = User.Role.AUDITOR
            existing.is_staff = False
            existing.is_superuser = False
            existing.save(update_fields=['role', 'is_staff', 'is_superuser'])
            self.stdout.write(self.style.SUCCESS(f'{username} is now role=auditor'))
            return

        if existing is not None:
            raise CommandError(
                f'User {username!r} already exists — use --convert to change its role'
            )

        password = secrets.token_urlsafe(12)
        User.objects.create_user(
            username=username,
            email=options['email'],
            password=password,
            first_name=options['first_name'],
            last_name=options['last_name'],
            role=User.Role.AUDITOR,
        )
        self.stdout.write(self.style.SUCCESS(f'Created auditor account {username!r}'))
        self.stdout.write(f'Temporary password: {password}')
        self.stdout.write('Share it over a secure channel and ask them to change it.')
