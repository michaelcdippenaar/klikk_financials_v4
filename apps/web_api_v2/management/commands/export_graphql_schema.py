from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.web_api_v2.schema import schema


class Command(BaseCommand):
    help = 'Export the Web API v2 GraphQL schema or verify its committed snapshot.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--check',
            action='store_true',
            help='Fail when the committed schema snapshot differs from the runtime schema.',
        )

    def handle(self, *args, **options):
        output_path = Path(settings.BASE_DIR) / 'apps/web_api_v2/schema.graphql'
        rendered = f'{schema.as_str().rstrip()}\n'
        if options['check']:
            if not output_path.exists() or output_path.read_text(encoding='utf-8') != rendered:
                raise CommandError(
                    'GraphQL schema snapshot is stale. Run manage.py export_graphql_schema.',
                )
            self.stdout.write(self.style.SUCCESS('GraphQL schema snapshot is current.'))
            return

        output_path.write_text(rendered, encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'Exported GraphQL schema to {output_path}'))
