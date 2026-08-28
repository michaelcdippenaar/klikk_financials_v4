"""Source evidence must be measured, scoped, and never confused with a run.

The pipeline read reports what V2 has executed. In production that ledger is
empty, so every stage rendered "Not run" while the entity held six figures of
trial-balance rows. These tests cover the other half — what data is present —
and the distinctions the 2026-08-22 postmortem requires be kept.
"""

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.web_api_v2.models import UserEntityMembership
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_cube.models import XeroTrailBalance
from apps.xero.xero_metadata.models import XeroAccount
from rest_framework_simplejwt.tokens import RefreshToken


QUERY = '''
query Pipeline($context: FinancialContextInput!) {
  xeroPipelineSummary(context: $context) {
    stages {
      key
      sourceEvidence { state label periodScoped recordCount latestRecordAt userSafeReason }
    }
  }
}
'''


class XeroSourceEvidenceTests(TestCase):
    def setUp(self):
        self.url = reverse('web_api_v2:graphql')
        self.user = get_user_model().objects.create_user(
            username='evidence-reviewer', password='safe-test-pass-5521',
        )
        # July fiscal start, so FY2026 runs 2025-07-01 .. 2026-06-30.
        self.entity = XeroTenant.objects.create(
            tenant_id='evidence-entity', tenant_name='Evidence (Pty) Ltd',
            fiscal_year_start_month=7,
        )
        self.other = XeroTenant.objects.create(
            tenant_id='evidence-other', tenant_name='Other (Pty) Ltd',
            fiscal_year_start_month=7,
        )
        UserEntityMembership.objects.create(
            user=self.user, entity=self.entity,
            role=UserEntityMembership.Role.VIEWER, active=True,
        )

    def _post(self, entity, selection):
        access = RefreshToken.for_user(self.user).access_token
        return self.client.post(
            self.url,
            data={
                'query': QUERY,
                'variables': {'context': {
                    'entityId': entity.pk, 'financialYear': 2026,
                    'periodSelection': selection,
                }},
            },
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {access}',
        )

    def _evidence(self, entity=None):
        response = self._post(
            entity or self.entity, {'mode': 'ALL'},
        )
        payload = response.json()
        self.assertNotIn('errors', payload, payload)
        return {
            stage['key']: stage['sourceEvidence']
            for stage in payload['data']['xeroPipelineSummary']['stages']
        }

    def _trial_balance_row(self, on, entity=None):
        entity = entity or self.entity
        account, _ = XeroAccount.objects.get_or_create(
            organisation=entity, code='200',
            defaults={'name': 'Sales', 'type': 'REVENUE'},
        )
        return XeroTrailBalance.objects.create(
            organisation=entity, account=account, date=on,
            year=on.year, month=on.month,
            fin_year=on.year + (1 if on.month >= 7 else 0), amount=0,
        )

    def test_an_entity_with_no_data_reports_absent_not_unavailable(self):
        """A measured zero is a fact. Not knowing is a different fact."""
        evidence = self._evidence()['TRIAL_BALANCE']
        self.assertEqual(evidence['state'], 'ABSENT')
        self.assertEqual(evidence['recordCount'], 0)
        self.assertIsNotNone(evidence['userSafeReason'])

    def test_rows_inside_the_resolved_window_are_counted(self):
        self._trial_balance_row(datetime.date(2025, 8, 31))
        self._trial_balance_row(datetime.date(2026, 5, 31))
        evidence = self._evidence()['TRIAL_BALANCE']
        self.assertEqual(evidence['state'], 'PRESENT')
        self.assertEqual(evidence['recordCount'], 2)
        self.assertTrue(evidence['periodScoped'])

    def test_rows_outside_the_resolved_window_are_excluded(self):
        """A count spanning all time when a year was selected is a lie."""
        self._trial_balance_row(datetime.date(2025, 8, 31))   # inside FY2026
        self._trial_balance_row(datetime.date(2024, 8, 31))   # before it
        self._trial_balance_row(datetime.date(2027, 8, 31))   # after it
        self.assertEqual(self._evidence()['TRIAL_BALANCE']['recordCount'], 1)

    def test_another_entity_rows_are_never_counted(self):
        self._trial_balance_row(datetime.date(2025, 8, 31), entity=self.other)
        self.assertEqual(self._evidence()['TRIAL_BALANCE']['recordCount'], 0)

    def test_reference_data_is_reported_entity_wide_and_says_so(self):
        """An entity has accounts, not accounts-for-March."""
        evidence = self._evidence()['METADATA']
        self.assertFalse(evidence['periodScoped'])
        self.assertEqual(evidence['label'], 'accounts')

    def test_every_stage_reports_evidence_with_a_user_facing_label(self):
        for key, evidence in self._evidence().items():
            with self.subTest(stage=key):
                self.assertIsNotNone(evidence, f'{key} returned no source evidence')
                self.assertTrue(evidence['label'])
                self.assertIn(evidence['state'], {'PRESENT', 'ABSENT', 'UNAVAILABLE'})

    def test_the_query_count_does_not_grow_with_selected_periods(self):
        """These are large tables; evidence must be aggregate-only.

        Measuring per stage per period would turn one page load into dozens of
        counts over six-figure tables.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._trial_balance_row(datetime.date(2025, 8, 31))

        with CaptureQueriesContext(connection) as twelve_months:
            self._post(self.entity, {'mode': 'ALL'})
        with CaptureQueriesContext(connection) as one_month:
            self._post(self.entity, {'mode': 'MONTHS', 'months': ['2025-08']})

        self.assertLessEqual(
            len(twelve_months) - len(one_month), 2,
            'source-evidence query count scales with the number of selected periods',
        )

    def test_cross_entity_access_is_refused(self):
        payload = self._post(self.other, {'mode': 'ALL'}).json()
        self.assertIn('errors', payload)
        self.assertEqual(payload['errors'][0]['extensions']['code'], 'FORBIDDEN_ENTITY')
