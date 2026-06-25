"""
Apply the out-of-band pgvector schema migration(s) for the rag/ store.

The rag/ `documents` table is not a Django model; it is provisioned via the
SQL scripts in apps/ai_agent/rag/sql/. This command applies them against the
Django default database connection (so it targets whichever environment the
running settings point at — staging, etc.).

Examples:
  python manage.py rag_migrate                 # apply all rag/sql/*.sql in order
  python manage.py rag_migrate --dry-run       # print what would run, do nothing
  python manage.py rag_migrate --file 0001_documents_bge_m3_1024.sql

After applying, repopulate vectors:
  python manage.py rag_reindex --full
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

SQL_DIR = Path(__file__).resolve().parents[2] / "rag" / "sql"


class Command(BaseCommand):
    help = "Apply out-of-band pgvector SQL migrations for the rag/ documents store."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            metavar="NAME",
            help="Apply a single SQL file from rag/sql/ (default: all *.sql in name order).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the SQL that would run without executing it.",
        )

    def handle(self, *args, **options):
        if not SQL_DIR.is_dir():
            raise CommandError(f"SQL directory not found: {SQL_DIR}")

        if options.get("file"):
            files = [SQL_DIR / options["file"]]
            if not files[0].is_file():
                raise CommandError(f"SQL file not found: {files[0]}")
        else:
            files = sorted(SQL_DIR.glob("*.sql"))
            if not files:
                raise CommandError(f"No *.sql files found in {SQL_DIR}")

        db = connection.settings_dict
        self.stdout.write(
            f"Target DB: {db.get('NAME')} @ {db.get('HOST')}:{db.get('PORT')} "
            f"(user={db.get('USER')})"
        )

        for sql_file in files:
            sql = sql_file.read_text(encoding="utf-8")
            self.stdout.write(f"\n--- {sql_file.name} ---")
            if options.get("dry_run"):
                self.stdout.write(sql)
                continue
            with connection.cursor() as cur:
                cur.execute(sql)
            self.stdout.write(self.style.SUCCESS(f"Applied {sql_file.name}"))

        if not options.get("dry_run"):
            self.stdout.write(
                self.style.WARNING(
                    "\nDone. Now repopulate vectors: python manage.py rag_reindex --full"
                )
            )
