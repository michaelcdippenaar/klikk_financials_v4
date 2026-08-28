import json
import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from apps.web_api_v2.models import (
    IngestProcessRun,
    IngestProcessRunPeriod,
    UserEntityCapability,
    UserEntityMembership,
)
from apps.investec.models import (
    InvestecBankAccount,
    InvestecBankTransaction,
    InvestecEntityAccount,
)
from apps.xero.xero_core.models import XeroTenant

# The tenant the seed migration binds to Klikk's Investec bank accounts. These
# tests used to read it out of a dict in the service; the binding is now a row.
BOUND_BANK_TENANT = '41ebfa0e-012e-4ff1-82ba-a9a7585c536c'


QUERY = '''
query OverviewSources($context: FinancialContextInput!) {
  overviewIngestSources(context: $context) {
    context {
      entityId financialYear fiscalYearStartMonth startsOn endsOn
      selectionMode selectedPeriods
    }
    actionableAttentionCount liveSourceCount unavailableSourceCount
    sources {
      key label provider mode availabilityCode state
      userSafeReason remediation
      latestAttemptAt latestSuccessAt freshnessAt
      records { read created updated skipped failed }
      outputs { code label count }
      validation { state code message }
      actions {
        kind permitted processKey requiredCapability disabledCode disabledReason
      }
      xeroPipelineAvailable
    }
  }
}
'''


class OverviewIngestSourcesTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='overview-reader',
            password='safe-test',
        )
        self.entity = XeroTenant.objects.create(
            tenant_id='overview-source-a',
            tenant_name='Overview Source A',
            fiscal_year_start_month=7,
        )
        self.other_entity = XeroTenant.objects.create(
            tenant_id='overview-source-b',
            tenant_name='Overview Source B',
            fiscal_year_start_month=7,
        )
        self.membership = UserEntityMembership.objects.create(
            user=self.user,
            entity=self.entity,
        )
        self.url = reverse('web_api_v2:graphql')
        self.token = str(AccessToken.for_user(self.user))

    def _context(self, entity=None, financial_year=2026, months=None, mode='MONTHS'):
        return {
            'entityId': (entity or self.entity).pk,
            'financialYear': financial_year,
            'periodSelection': {
                'mode': mode,
                'months': ['2025-07'] if months is None and mode == 'MONTHS' else months,
            },
        }

    def _query(self, context=None):
        return self.client.post(
            self.url,
            data=json.dumps({
                'query': QUERY,
                'operationName': 'OverviewSources',
                'variables': {'context': context or self._context()},
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

    def _data(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('errors', response.json())
        return response.json()['data']['overviewIngestSources']

    def _create_run(self, process_key, *, records=None):
        run = IngestProcessRun.objects.create(
            entity=self.entity,
            actor=self.user,
            process_key=process_key,
            state=IngestProcessRun.State.SUCCEEDED,
            idempotency_key=f'overview-{process_key}',
            request_fingerprint='a' * 64,
            periods=['2025-07'],
            records_summary=records or {},
            started_at=timezone.now(),
            finished_at=timezone.now(),
        )
        IngestProcessRunPeriod.objects.create(run=run, period='2025-07')
        return run

    def _create_investec_transaction(
        self,
        account,
        *,
        transaction_date,
        amount='1.00',
    ):
        return InvestecBankTransaction.objects.create(
            account=account,
            type=InvestecBankTransaction.TYPE_DEBIT,
            status=InvestecBankTransaction.STATUS_POSTED,
            transaction_date=transaction_date,
            amount=amount,
        )

    @patch('apps.web_api_v2.queries.xero_pipeline.prerequisite_status', return_value=[])
    @patch('apps.web_api_v2.queries.xero_pipeline.has_tenant_credentials', return_value=True)
    def test_exact_catalogue_and_ending_year_context(self, credentials, prerequisites):
        data = self._data(self._query())
        self.assertEqual(data['context'], {
            'entityId': self.entity.pk,
            'financialYear': 2026,
            'fiscalYearStartMonth': 7,
            'startsOn': '2025-07-01',
            'endsOn': '2026-06-30',
            'selectionMode': 'MONTHS',
            'selectedPeriods': ['2025-07'],
        })
        self.assertEqual(
            [source['key'] for source in data['sources']],
            [
                'XERO',
                'INVESTEC_BANK',
                'INVESTMENT_HOLDINGS',
                'SHARE_TRANSACTIONS',
                'WHATSAPP_RECEIPTS',
                'EMAIL_DOCUMENTS',
                'MANUAL_DOCUMENT_UPLOADS',
                'PLANNING_ANALYTICS_TARGETS',
            ],
        )
        self.assertEqual(data['liveSourceCount'], 1)
        unavailable = data['sources'][1:]
        self.assertEqual(len(unavailable), 7)
        for source in unavailable:
            self.assertNotEqual(source['availabilityCode'], 'AVAILABLE')
            self.assertEqual(source['state'], 'UNAVAILABLE')
            self.assertIsNone(source['records'])
            self.assertIsNone(source['latestAttemptAt'])
            self.assertIsNone(source['latestSuccessAt'])
            self.assertIsNone(source['freshnessAt'])
            self.assertEqual(source['outputs'], [])
            self.assertEqual(source['validation']['state'], 'UNAVAILABLE')
            self.assertTrue(source['userSafeReason'])
            self.assertTrue(all(not action['permitted'] for action in source['actions']))

    @patch('apps.web_api_v2.queries.xero_pipeline.prerequisite_status', return_value=[])
    @patch('apps.web_api_v2.queries.xero_pipeline.has_tenant_credentials', return_value=True)
    def test_available_measured_zero_is_preserved_and_run_is_grant_gated(
        self,
        credentials,
        prerequisites,
    ):
        for process_key in (
            'metadata',
            'transaction-journal-sync',
            'invoice-sync',
            'process-journals',
        ):
            self._create_run(process_key)
        self._create_run(
            'trail-balance',
            records={'read': 0, 'created': 0, 'updated': 0, 'skipped': 0, 'failed': 0},
        )
        data = self._data(self._query())
        xero = data['sources'][0]
        self.assertEqual(xero['availabilityCode'], 'AVAILABLE')
        self.assertEqual(xero['state'], 'CURRENT')
        self.assertEqual(xero['validation']['state'], 'PASSED')
        self.assertEqual(
            xero['records'],
            {'read': 0, 'created': 0, 'updated': 0, 'skipped': 0, 'failed': 0},
        )
        self.assertEqual([item['count'] for item in xero['outputs']], [0, 0, 0, 0, 0])
        run_action = next(
            action for action in xero['actions'] if action['kind'] == 'RUN'
        )
        self.assertFalse(run_action['permitted'])
        self.assertEqual(
            run_action['requiredCapability'],
            UserEntityCapability.Code.RUN_INGESTION_PROCESS,
        )

        UserEntityCapability.objects.create(
            membership=self.membership,
            code=UserEntityCapability.Code.RUN_INGESTION_PROCESS,
            granted_by=self.user,
        )
        xero = self._data(self._query())['sources'][0]
        run_action = next(
            action for action in xero['actions'] if action['kind'] == 'RUN'
        )
        self.assertTrue(run_action['permitted'])

    def test_unavailable_xero_never_copies_stale_evidence(self):
        self.entity.reauth_required = True
        self.entity.save(update_fields=('reauth_required',))
        self._create_run('trail-balance', records={'read': 91})
        data = self._data(self._query())
        xero = data['sources'][0]
        self.assertNotEqual(xero['availabilityCode'], 'AVAILABLE')
        self.assertIsNone(xero['records'])
        self.assertIsNone(xero['latestAttemptAt'])
        self.assertIsNone(xero['latestSuccessAt'])
        self.assertEqual(xero['outputs'], [])
        self.assertEqual(xero['validation']['state'], 'UNAVAILABLE')

    def test_cross_entity_and_out_of_year_context_are_typed(self):
        forbidden = self._query(self._context(entity=self.other_entity))
        self.assertEqual(
            forbidden.json()['errors'][0]['extensions']['code'],
            'FORBIDDEN_ENTITY',
        )
        invalid = self._query(self._context(months=['2026-07']))
        error = invalid.json()['errors'][0]
        self.assertEqual(error['extensions']['code'], 'VALIDATION_ERROR')
        self.assertEqual(
            error['extensions']['correlationId'],
            invalid['X-Correlation-ID'],
        )

    @patch('apps.web_api_v2.queries.xero_pipeline.prerequisite_status', return_value=[])
    @patch('apps.web_api_v2.queries.xero_pipeline.has_tenant_credentials', return_value=True)
    def test_all_mode_echoes_twelve_server_resolved_months(self, credentials, prerequisites):
        data = self._data(self._query(self._context(mode='ALL', months=None)))
        self.assertEqual(
            data['context']['selectedPeriods'],
            [
                '2025-07', '2025-08', '2025-09', '2025-10', '2025-11', '2025-12',
                '2026-01', '2026-02', '2026-03', '2026-04', '2026-05', '2026-06',
            ],
        )

    @patch('apps.web_api_v2.queries.xero_pipeline.prerequisite_status', return_value=[])
    @patch('apps.web_api_v2.queries.xero_pipeline.has_tenant_credentials', return_value=True)
    def test_bound_investec_bank_status_is_period_scoped_and_entity_safe(
        self,
        credentials,
        prerequisites,
    ):
        bound_entity = XeroTenant.objects.create(
            tenant_id=BOUND_BANK_TENANT,
            tenant_name='Klikk (Pty) Ltd',
            fiscal_year_start_month=7,
        )
        UserEntityMembership.objects.create(user=self.user, entity=bound_entity)
        # The binding is a row now, not a dict. A tenant created inside a test
        # was not present when the seed migration ran, so it binds its own.
        InvestecEntityAccount.objects.create(
            entity=bound_entity, account_number='10011924075',
            kind=InvestecEntityAccount.Kind.BANK, active=True,
        )
        klikk_account = InvestecBankAccount.objects.create(
            account_id='investec-klikk-account',
            account_number='10011924075',
            owner='Klikk',
        )
        other_account = InvestecBankAccount.objects.create(
            account_id='investec-other-account',
            account_number='10013017883',
            owner='MLD Trust',
        )
        selected = self._create_investec_transaction(
            klikk_account,
            transaction_date=datetime.date(2025, 7, 15),
        )
        self._create_investec_transaction(
            klikk_account,
            transaction_date=datetime.date(2025, 8, 15),
        )
        self._create_investec_transaction(
            other_account,
            transaction_date=datetime.date(2025, 7, 15),
        )
        freshness = timezone.now() - datetime.timedelta(hours=2)
        InvestecBankTransaction.objects.filter(pk=selected.pk).update(
            updated_at=freshness,
        )
        before = InvestecBankTransaction.objects.count()

        data = self._data(self._query(self._context(entity=bound_entity)))
        investec = data['sources'][1]

        self.assertEqual(investec['key'], 'INVESTEC_BANK')
        self.assertEqual(investec['availabilityCode'], 'AVAILABLE')
        self.assertEqual(investec['state'], 'READY')
        self.assertEqual(
            investec['records'],
            {'read': 1, 'created': None, 'updated': None, 'skipped': None, 'failed': None},
        )
        self.assertEqual(
            datetime.datetime.fromisoformat(investec['freshnessAt']),
            freshness,
        )
        self.assertIsNone(investec['latestAttemptAt'])
        self.assertIsNone(investec['latestSuccessAt'])
        self.assertEqual(investec['validation']['state'], 'NOT_RUN')
        self.assertTrue(all(not action['permitted'] for action in investec['actions']))
        self.assertEqual(data['liveSourceCount'], 2)
        self.assertEqual(InvestecBankTransaction.objects.count(), before)

    @patch('apps.web_api_v2.queries.xero_pipeline.prerequisite_status', return_value=[])
    @patch('apps.web_api_v2.queries.xero_pipeline.has_tenant_credentials', return_value=True)
    def test_bound_investec_bank_preserves_measured_zero_without_claiming_freshness(
        self,
        credentials,
        prerequisites,
    ):
        bound_entity = XeroTenant.objects.create(
            tenant_id=BOUND_BANK_TENANT,
            tenant_name='Klikk (Pty) Ltd',
            fiscal_year_start_month=7,
        )
        UserEntityMembership.objects.create(user=self.user, entity=bound_entity)
        # The binding is a row now, not a dict. A tenant created inside a test
        # was not present when the seed migration ran, so it binds its own.
        InvestecEntityAccount.objects.create(
            entity=bound_entity, account_number='10011924075',
            kind=InvestecEntityAccount.Kind.BANK, active=True,
        )
        InvestecBankAccount.objects.create(
            account_id='investec-klikk-empty',
            account_number='10011924075',
            owner='Klikk',
        )

        investec = self._data(
            self._query(self._context(entity=bound_entity))
        )['sources'][1]

        self.assertEqual(investec['availabilityCode'], 'AVAILABLE')
        self.assertEqual(investec['records']['read'], 0)
        self.assertIsNone(investec['freshnessAt'])

    @patch('apps.web_api_v2.queries.xero_pipeline.prerequisite_status', return_value=[])
    @patch('apps.web_api_v2.queries.xero_pipeline.has_tenant_credentials', return_value=True)
    def test_investec_binding_is_exact_and_missing_accounts_are_not_configured(
        self,
        credentials,
        prerequisites,
    ):
        bound_entity = XeroTenant.objects.create(
            tenant_id=BOUND_BANK_TENANT,
            tenant_name='Klikk (Pty) Ltd',
            fiscal_year_start_month=7,
        )
        lookalike = XeroTenant.objects.create(
            tenant_id='lookalike-klikk',
            tenant_name='Klikk (Pty) Ltd',
            fiscal_year_start_month=7,
        )
        UserEntityMembership.objects.create(user=self.user, entity=bound_entity)
        # The binding is a row now, not a dict. A tenant created inside a test
        # was not present when the seed migration ran, so it binds its own.
        InvestecEntityAccount.objects.create(
            entity=bound_entity, account_number='10011924075',
            kind=InvestecEntityAccount.Kind.BANK, active=True,
        )
        UserEntityMembership.objects.create(user=self.user, entity=lookalike)

        missing = self._data(
            self._query(self._context(entity=bound_entity))
        )['sources'][1]
        unbound = self._data(
            self._query(self._context(entity=lookalike))
        )['sources'][1]

        self.assertEqual(missing['availabilityCode'], 'NOT_CONFIGURED')
        self.assertIsNone(missing['records'])
        self.assertEqual(unbound['availabilityCode'], 'ENTITY_BINDING_REQUIRED')
        self.assertIsNone(unbound['records'])
    @patch("apps.web_api_v2.queries.xero_pipeline.prerequisite_status", return_value=[])
    @patch("apps.web_api_v2.queries.xero_pipeline.has_tenant_credentials", return_value=True)
    def test_xero_card_does_not_attribute_partial_multi_period_counts(
        self, credentials, prerequisites,
    ):
        run = self._create_run("trail-balance", records={"read": 88})
        run.periods = ["2025-07", "2025-08"]
        run.save(update_fields=("periods",))
        IngestProcessRunPeriod.objects.create(run=run, period="2025-08")
        xero = self._data(self._query())["sources"][0]
        self.assertIsNone(xero["records"])
        self.assertEqual(xero["outputs"], [])
        full = self._context(months=["2025-07", "2025-08"])
        xero = self._data(self._query(full))["sources"][0]
        self.assertEqual(xero["records"]["read"], 88)
