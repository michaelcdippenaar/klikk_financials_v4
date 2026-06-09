"""Seed Category + ClassificationRule rows from data/clasification_rules.csv.

Idempotent — safe to re-run. Categories are keyed by (type, name); rules by
(tag, transaction_type). Re-seed after editing the CSV to add/refresh rules.

    python manage.py seed_personal_categories --dry-run
    python manage.py seed_personal_categories
"""
import csv
import os
from collections import Counter

from django.core.management.base import BaseCommand

from apps.personal_expenses.models import Category, ClassificationRule

APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RULES_CSV = os.path.join(APP_DIR, 'data', 'clasification_rules.csv')


class Command(BaseCommand):
    help = "Seed Category + ClassificationRule from data/clasification_rules.csv (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument('--csv', default=RULES_CSV, help="Path to the rules CSV (';'-delimited).")
        parser.add_argument('--dry-run', action='store_true', help="Report the plan; write nothing.")

    def handle(self, *args, **opts):
        path = opts['csv']
        if not os.path.exists(path):
            self.stderr.write(self.style.ERROR(f'Rules file not found: {path}'))
            return

        with open(path, newline='') as fh:
            rows = list(csv.DictReader(fh, delimiter=';'))

        categories = set()
        by_type = Counter()
        valid = 0
        for row in rows:
            tag = (row.get('tag') or '').strip().upper()
            if not tag:
                continue
            valid += 1
            ctype = (row.get('type') or '').strip() or Category.TYPE_REVIEW
            name = (row.get('category') or '').strip() or 'Uncategorised'
            categories.add((ctype, name))
            by_type[ctype] += 1

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING(
                f'[DRY-RUN] {len(rows)} rows, {valid} valid rules, {len(categories)} categories.'))
            for ctype, count in sorted(by_type.items()):
                self.stdout.write(f'  {ctype:<10} {count} rules')
            self.stdout.write('  (re-run without --dry-run to write)')
            return

        cat_objs = {}
        n_cat = 0
        for ctype, name in categories:
            cat, created = Category.objects.get_or_create(type=ctype, name=name)
            cat_objs[(ctype, name)] = cat
            n_cat += int(created)

        n_new = n_upd = 0
        for row in rows:
            tag = (row.get('tag') or '').strip().upper()
            if not tag:
                continue
            ctype = (row.get('type') or '').strip() or Category.TYPE_REVIEW
            name = (row.get('category') or '').strip() or 'Uncategorised'
            tt = (row.get('transaction_type') or '').strip()
            _obj, created = ClassificationRule.objects.update_or_create(
                tag=tag, transaction_type=tt,
                defaults={'category': cat_objs[(ctype, name)], 'is_active': True},
            )
            n_new += int(created)
            n_upd += int(not created)

        self.stdout.write(self.style.SUCCESS(
            f'Seed complete: +{n_cat} categories, +{n_new} new rules, {n_upd} rules refreshed.'))
