from django.core.management.base import BaseCommand, CommandError

from apps.web_api_v2.models import IngestSourceJobDefinition
from apps.web_api_v2.services.ingest_registry import PROCESS_DEFINITIONS, provision_definition_defaults
from apps.xero.xero_core.models import XeroTenant


class Command(BaseCommand):
    help = 'Preview or provision default v2 ingest source definitions. Never grants execution.'

    def add_arguments(self, parser):
        parser.add_argument('--entity-id')
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Persist missing definitions. Without this flag the command is dry-run only.',
        )

    def handle(self, *args, **options):
        entities = XeroTenant.objects.order_by('tenant_id')
        if options['entity_id']:
            entities = entities.filter(pk=options['entity_id'])
            if not entities.exists():
                raise CommandError('Entity not found.')

        expected_keys = set(PROCESS_DEFINITIONS) - {'standard-sync'}
        missing_total = 0
        created_total = 0
        for entity in entities:
            present = set(IngestSourceJobDefinition.objects.filter(
                entity=entity,
                key__in=expected_keys,
            ).values_list('key', flat=True))
            missing = len(expected_keys - present)
            missing_total += missing
            if options['apply'] and missing:
                created_total += provision_definition_defaults(entity)

        mode = 'APPLIED' if options['apply'] else 'DRY RUN'
        self.stdout.write(
            f'{mode}: entities={entities.count()} missing={missing_total} created={created_total}'
        )
        if not options['apply']:
            self.stdout.write('No rows changed. Re-run with --apply to provision missing definitions.')
