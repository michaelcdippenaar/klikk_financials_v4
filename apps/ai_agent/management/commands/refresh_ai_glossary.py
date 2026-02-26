"""
Process pending glossary refresh request (set when Xero accounts/contacts change)
and optionally re-vectorize the default corpus so the AI agent's knowledge stays up to date.

Run from cron every 1–5 minutes, or after Xero sync:
  python manage.py refresh_ai_glossary
  python manage.py refresh_ai_glossary --vectorize   # also re-embed corpus
"""
from django.core.management.base import BaseCommand

from apps.ai_agent.models import GlossaryRefreshRequest
from apps.ai_agent.services.glossary_builder import refresh_glossary_documents
from apps.ai_agent.services.vector_store import vectorize_corpus_documents


class Command(BaseCommand):
    help = 'Refresh account/contact glossary docs from Xero metadata and optionally re-vectorize.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--vectorize',
            action='store_true',
            help='After refreshing glossary, re-vectorize the default corpus for each project.',
        )
        parser.add_argument(
            '--project',
            type=int,
            default=None,
            help='Limit to this project id. Default: all projects with default_corpus.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Run refresh even if no pending request (e.g. first-time setup).',
        )

    def handle(self, *args, **options):
        run_vectorize = options['vectorize']
        project_id = options['project']
        force = options['force']

        req = None
        try:
            req = GlossaryRefreshRequest.objects.filter(pk=1).first()
        except Exception:
            pass

        if not force and req is None:
            self.stdout.write('No glossary refresh requested. Use --force to run anyway.')
            return

        org_id = req.organisation_id if req else None
        self.stdout.write('Refreshing glossary documents from Xero accounts and contacts...')
        updated = refresh_glossary_documents(project_id=project_id, organisation_id=org_id)
        self.stdout.write(self.style.SUCCESS(f'Updated {updated} glossary doc(s).'))

        if req:
            req.delete()

        if run_vectorize:
            from apps.ai_agent.models import AgentProject
            projects = AgentProject.objects.filter(default_corpus__isnull=False)
            if project_id is not None:
                projects = projects.filter(id=project_id)
            for project in projects:
                try:
                    result = vectorize_corpus_documents(
                        corpus=project.default_corpus,
                        project_id=project.id,
                        force=True,
                    )
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Vectorized project {project.slug}: {result.documents_seen} docs, {result.chunks_written} chunks.'
                        )
                    )
                except Exception as e:
                    self.stderr.write(self.style.ERROR(f'Vectorize failed for {project.slug}: {e}'))
