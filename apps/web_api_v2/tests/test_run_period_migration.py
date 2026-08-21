from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TransactionTestCase

from apps.web_api_v2.models import IngestProcessRun, IngestProcessRunPeriod
from apps.xero.xero_core.models import XeroTenant


migration = import_module(
    'apps.web_api_v2.migrations.0003_ingestprocessrunperiod'
)


class IngestProcessRunPeriodMigrationTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='period-migration',
            password='safe-test',
        )
        self.entity = XeroTenant.objects.create(
            tenant_id='period-migration',
            tenant_name='Period Migration',
        )

    def _run(self, suffix, periods):
        return IngestProcessRun.objects.create(
            entity=self.entity,
            actor=self.user,
            process_key='metadata',
            state=IngestProcessRun.State.SUCCEEDED,
            idempotency_key=f'period-{suffix}',
            request_fingerprint=suffix[0] * 64,
            periods=periods,
        )

    def test_backfill_accepts_only_explicit_canonical_periods_and_is_idempotent(self):
        valid = self._run(
            'valid',
            ['2025-07', '2025-07', '2026-06', '2026-13', 202507, None],
        )
        empty = self._run('empty', [])
        non_list = self._run('mapping', {'period': '2025-07'})

        migration.backfill_explicit_run_periods(apps, None)
        migration.backfill_explicit_run_periods(apps, None)

        self.assertEqual(
            list(valid.run_periods.values_list('period', flat=True)),
            ['2025-07', '2026-06'],
        )
        self.assertFalse(empty.run_periods.exists())
        self.assertFalse(non_list.run_periods.exists())

    def test_model_validation_and_uniqueness(self):
        run = self._run('constraint', [])
        invalid = IngestProcessRunPeriod(run=run, period='2025-13')
        with self.assertRaises(ValidationError):
            invalid.full_clean()

        IngestProcessRunPeriod.objects.create(run=run, period='2025-07')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                IngestProcessRunPeriod.objects.create(run=run, period='2025-07')
