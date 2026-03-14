"""
Run RAG indexing from Django management command.

Examples:
  python manage.py rag_reindex
  python manage.py rag_reindex --docs-only
  python manage.py rag_reindex --full
  python manage.py rag_reindex --schema
  python manage.py rag_reindex --elements
  python manage.py rag_reindex --element-dims account entity
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from apps.ai_agent.rag import indexer


class Command(BaseCommand):
    help = 'Re-index RAG documents/metadata into pgvector.'

    def add_arguments(self, parser):
        parser.add_argument('--full', action='store_true', help='Re-index everything (docs + TM1 dims + elements + schema).')
        parser.add_argument('--docs-only', action='store_true', help='Only index documentation markdown files.')
        parser.add_argument('--tm1-only', action='store_true', help='Only index TM1 dimension-level metadata.')
        parser.add_argument('--schema', action='store_true', help='Index PostgreSQL table schemas and data context.')
        parser.add_argument('--elements', action='store_true', help='Index per-element profiles for key dimensions.')
        parser.add_argument('--element-dims', nargs='*', metavar='DIM', help='Index per-element profiles for specific dimensions.')
        parser.add_argument('--pg-host', metavar='HOST', help='Override PostgreSQL host (e.g. localhost when running outside Docker).')

    def handle(self, *args, **options):
        argv = ['indexer']
        if options.get('full'):
            argv.append('--full')
        if options.get('docs_only'):
            argv.append('--docs-only')
        if options.get('tm1_only'):
            argv.append('--tm1-only')
        if options.get('schema'):
            argv.append('--schema')
        if options.get('elements'):
            argv.append('--elements')
        dims = options.get('element_dims') or []
        if dims:
            argv.extend(['--element-dims', *dims])
        pg_host = options.get('pg_host')
        if pg_host:
            argv.extend(['--pg-host', pg_host])

        self.stdout.write(f"Running RAG reindex with args: {' '.join(argv[1:]) or '(default)'}")

        # Reuse the existing indexer entrypoint so behavior stays consistent.
        original_argv = sys.argv
        try:
            sys.argv = argv
            indexer.main()
        finally:
            sys.argv = original_argv

