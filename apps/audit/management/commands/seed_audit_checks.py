"""
Upsert the v1 registry (apps/audit/seed_checks.py) into audit.checks.

    python manage.py seed_audit_checks            # validate (EXPLAIN) + upsert all 45
    python manage.py seed_audit_checks --no-explain
    python manage.py seed_audit_checks --only BAL-01,BAL-02

Existing rows are updated (title/description/sql/…); rows MC has deactivated
stay deactivated (``active`` is not overwritten on update).
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.audit.models import AuditCheck
from apps.audit.seed_checks import CHECKS, SOURCE
from apps.audit.services import SqlGuardError, validate_sql


class Command(BaseCommand):
    help = 'Upsert the seed year-end audit checks into audit.checks (validates SQL first).'

    def add_arguments(self, parser):
        parser.add_argument('--only', help='Comma-separated check codes to seed.')
        parser.add_argument('--no-explain', action='store_true', help='Skip EXPLAIN validation.')

    def handle(self, *args, **opts):
        only = {c.strip().upper() for c in (opts.get('only') or '').split(',') if c.strip()}
        explain = not opts.get('no_explain')
        created = updated = 0
        errors = []
        for spec in CHECKS:
            if only and spec['code'] not in only:
                continue
            sql = spec['sql_text'].strip()
            try:
                validate_sql(sql, explain=explain)
            except SqlGuardError as exc:
                errors.append((spec['code'], str(exc)))
                self.stderr.write(self.style.ERROR(f"{spec['code']}: {exc}"))
                continue
            defaults = {
                'title': spec['title'], 'category': spec['category'], 'severity': spec['severity'],
                'description': spec['description'].strip(), 'rationale': spec.get('rationale', '').strip(),
                'sql_text': sql, 'expected': spec['expected'], 'owner_action': spec.get('owner_action', ''),
                'source': SOURCE, 'updated_at': timezone.now(),
            }
            obj, was_created = AuditCheck.objects.update_or_create(code=spec['code'], defaults=defaults)
            if was_created:
                created += 1
            else:
                updated += 1
        self.stdout.write(self.style.SUCCESS(f'seeded: created={created} updated={updated} errors={len(errors)}'))
        if errors:
            raise CommandError('some checks failed validation: ' + ', '.join(c for c, _ in errors))
