"""
Adversarial tests for get_api_call_stats() and GET /xero/sync/api-call-stats/.

No Xero client is ever constructed here; everything is DB rows + the DRF test
client, so there is no network path at all.

Usage:
    python manage.py test apps.xero.xero_sync.test_api_call_stats -v 2

NOTE: lives as test_api_call_stats.py (not tests/...) because a `tests/`
package next to the existing apps/xero/xero_sync/tests.py would shadow that
module and silently drop its test cases from discovery.
"""
import datetime

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.xero.xero_core.models import XeroApiQuota, XeroTenant
from apps.xero.xero_sync.api_call_logging import get_api_call_stats, log_xero_api_calls
from apps.xero.xero_sync.models import XeroApiCallLog

NOW = datetime.datetime(2026, 8, 19, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _quota(tenant_id, day=None, minute=None, status=200, seen_at=NOW):
    return XeroApiQuota.objects.create(
        tenant_id=tenant_id, day_remaining=day, min_remaining=minute,
        last_status=status, seen_at=seen_at,
    )


class ApiCallStatsByProcessTests(TestCase):
    """The under-count fix: invoices / quotes / documents must be counted."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant = XeroTenant.objects.create(tenant_id='t-a', tenant_name='A')

    def test_new_processes_are_in_process_choices(self):
        keys = {k for k, _ in XeroApiCallLog.PROCESS_CHOICES}
        self.assertTrue({'invoices', 'quotes', 'documents'} <= keys, keys)

    def test_invoices_row_counted_in_by_process_and_total_today(self):
        log_xero_api_calls('invoices', 7, tenant=self.tenant)
        stats = get_api_call_stats()
        self.assertIn('invoices', stats['by_process'])
        self.assertEqual(stats['by_process']['invoices'], {'last_run': 7, 'today': 7})
        self.assertEqual(stats['total_today'], 7)

    def test_quotes_and_documents_counted_and_summed(self):
        log_xero_api_calls('quotes', 3, tenant=self.tenant)
        log_xero_api_calls('documents', 19, tenant=self.tenant)
        log_xero_api_calls('documents', 1, tenant=self.tenant)
        stats = get_api_call_stats()
        self.assertEqual(stats['by_process']['quotes']['today'], 3)
        self.assertEqual(stats['by_process']['documents']['today'], 20)
        self.assertEqual(stats['by_process']['documents']['last_run'], 1)
        self.assertEqual(stats['total_today'], 23)

    def test_total_today_sums_every_listed_process(self):
        for key, _ in XeroApiCallLog.PROCESS_CHOICES:
            log_xero_api_calls(key, 2, tenant=self.tenant)
        stats = get_api_call_stats()
        self.assertEqual(stats['total_today'], 2 * len(XeroApiCallLog.PROCESS_CHOICES))
        self.assertEqual(set(stats['by_process']), {k for k, _ in XeroApiCallLog.PROCESS_CHOICES})

    def test_unlisted_process_is_stored_but_not_counted(self):
        # Documented behaviour (docstring on log_xero_api_calls). Locking it in
        # so a future rename of a process key fails loudly here.
        XeroApiCallLog.objects.create(process='not-a-process', tenant=self.tenant, api_calls=99)
        stats = get_api_call_stats()
        self.assertNotIn('not-a-process', stats['by_process'])
        self.assertEqual(stats['total_today'], 0)

    def test_yesterday_rows_not_in_today_but_are_last_run(self):
        old = XeroApiCallLog.objects.create(process='invoices', tenant=self.tenant, api_calls=40)
        XeroApiCallLog.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=2)
        )
        stats = get_api_call_stats()
        self.assertEqual(stats['by_process']['invoices']['today'], 0)
        self.assertEqual(stats['by_process']['invoices']['last_run'], 40)
        self.assertEqual(stats['total_today'], 0)

    def test_tenant_filter_applies_to_by_process(self):
        other = XeroTenant.objects.create(tenant_id='t-b', tenant_name='B')
        log_xero_api_calls('invoices', 5, tenant=self.tenant)
        log_xero_api_calls('invoices', 50, tenant=other)
        log_xero_api_calls('quotes', 1, tenant=None)  # untenanted row
        a = get_api_call_stats(tenant_id='t-a')
        b = get_api_call_stats(tenant_id='t-b')
        self.assertEqual(a['total_today'], 5)
        self.assertEqual(b['total_today'], 50)
        self.assertEqual(get_api_call_stats()['total_today'], 56)


class ApiCallStatsQuotaSelectionTests(TestCase):

    def test_no_rows_all_none_except_cap(self):
        stats = get_api_call_stats()
        cap = stats['cap']
        self.assertIsInstance(cap, int)
        self.assertEqual(stats['quota'], {
            'cap': cap, 'day_remaining': None, 'min_remaining': None,
            'used_estimate': None, 'seen_at': None, 'last_status': None, 'tenant_id': None,
        })

    def test_requested_tenant_row_is_used(self):
        _quota('t-old', day=10, seen_at=NOW + datetime.timedelta(hours=1))  # more recent
        _quota('t-req', day=700, minute=55, status=200, seen_at=NOW)
        q = get_api_call_stats(tenant_id='t-req')['quota']
        self.assertEqual(q['tenant_id'], 't-req')
        self.assertEqual(q['day_remaining'], 700)
        self.assertEqual(q['min_remaining'], 55)
        self.assertEqual(q['seen_at'], NOW.isoformat())

    def test_no_tenant_id_picks_most_recent_seen_at_not_pk_or_insert_order(self):
        # Insert order and PK sort order BOTH disagree with seen_at order.
        _quota('zzz', day=1, seen_at=NOW - datetime.timedelta(minutes=5))   # inserted first, last by PK
        _quota('aaa', day=2, seen_at=NOW - datetime.timedelta(minutes=10))  # first by PK
        _quota('mmm', day=3, seen_at=NOW)                                   # newest
        _quota('nnn', day=4, seen_at=NOW - datetime.timedelta(minutes=1))   # inserted last
        q = get_api_call_stats()['quota']
        self.assertEqual(q['tenant_id'], 'mmm')
        self.assertEqual(q['day_remaining'], 3)

    def test_tenant_id_with_no_row_does_not_fall_back_to_another_tenant(self):
        _quota('t-other', day=123, minute=5, status=200)
        q = get_api_call_stats(tenant_id='t-nope')['quota']
        self.assertIsNone(q['tenant_id'])
        self.assertIsNone(q['day_remaining'])
        self.assertIsNone(q['min_remaining'])
        self.assertIsNone(q['used_estimate'])
        self.assertIsNone(q['seen_at'])
        self.assertIsNone(q['last_status'])
        self.assertIsInstance(q['cap'], int)

    def test_garbage_tenant_id_values(self):
        _quota('t-other', day=123)
        for garbage in ("' OR 1=1 --", 'x' * 500, '☃', 't-other '):
            with self.subTest(garbage=garbage[:20]):
                q = get_api_call_stats(tenant_id=garbage)['quota']
                self.assertIsNone(q['tenant_id'])
                self.assertIsNone(q['day_remaining'])

    def test_quota_is_independent_of_call_log_tenant_filter(self):
        # quota row exists for t-x but NO call-log rows; by_process is all
        # zero, quota must still be populated.
        _quota('t-x', day=400)
        stats = get_api_call_stats(tenant_id='t-x')
        self.assertEqual(stats['total_today'], 0)
        self.assertEqual(stats['quota']['day_remaining'], 400)


class ApiCallStatsUsedEstimateTests(TestCase):

    @override_settings(XERO_DAILY_CALL_CAP=250)
    def test_cap_and_used_estimate_follow_setting(self):
        _quota('t', day=200)
        stats = get_api_call_stats()
        self.assertEqual(stats['cap'], 250)
        self.assertEqual(stats['quota']['cap'], 250)
        self.assertEqual(stats['quota']['used_estimate'], 50)

    @override_settings(XERO_DAILY_CALL_CAP=250)
    def test_cap_follows_setting_even_with_no_rows(self):
        stats = get_api_call_stats()
        self.assertEqual(stats['cap'], 250)
        self.assertEqual(stats['quota']['cap'], 250)

    @override_settings(XERO_DAILY_CALL_CAP=1000)
    def test_day_remaining_zero_means_used_equals_cap(self):
        _quota('t', day=0, minute=0)
        q = get_api_call_stats()['quota']
        self.assertEqual(q['day_remaining'], 0)
        self.assertEqual(q['used_estimate'], 1000)

    @override_settings(XERO_DAILY_CALL_CAP=1000)
    def test_day_remaining_above_cap_clamps_to_zero(self):
        _quota('t', day=4950)  # cap setting wrong or Xero raised the tenant limit
        q = get_api_call_stats()['quota']
        self.assertEqual(q['day_remaining'], 4950)
        self.assertEqual(q['used_estimate'], 0)

    @override_settings(XERO_DAILY_CALL_CAP=1000)
    def test_day_remaining_equal_cap_is_zero_used(self):
        _quota('t', day=1000)
        self.assertEqual(get_api_call_stats()['quota']['used_estimate'], 0)

    def test_day_remaining_none_row_gives_used_none_not_cap(self):
        _quota('t', day=None, minute=12)
        q = get_api_call_stats()['quota']
        self.assertEqual(q['tenant_id'], 't')
        self.assertIsNone(q['day_remaining'])
        self.assertIsNone(q['used_estimate'])
        self.assertEqual(q['min_remaining'], 12)

    @override_settings(XERO_DAILY_CALL_CAP=1000)
    def test_used_estimate_with_exhausted_budget_and_429(self):
        _quota('t', day=0, minute=0, status=429)
        q = get_api_call_stats()['quota']
        self.assertEqual(q['last_status'], 429)
        self.assertEqual(q['used_estimate'], 1000)


class ApiCallStatsEndpointTests(TestCase):

    def setUp(self):
        self.api = APIClient()
        self.url = reverse('xero_sync:xero-api-call-stats')

    def test_url_is_the_documented_path(self):
        self.assertEqual(self.url, '/xero/sync/api-call-stats/')

    def test_endpoint_returns_function_output_verbatim_as_json(self):
        tenant = XeroTenant.objects.create(tenant_id='t-e', tenant_name='E')
        log_xero_api_calls('invoices', 11, tenant=tenant)
        _quota('t-e', day=946, minute=58, status=200, seen_at=NOW)
        r = self.api.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'].split(';')[0], 'application/json')
        body = r.json()
        self.assertEqual(body, get_api_call_stats())
        self.assertEqual(set(body), {'cap', 'by_process', 'total_today', 'quota'})
        self.assertEqual(body['by_process']['invoices']['today'], 11)
        self.assertEqual(body['total_today'], 11)
        q = body['quota']
        self.assertEqual(q['day_remaining'], 946)
        self.assertEqual(q['used_estimate'], body['cap'] - 946)
        self.assertEqual(q['tenant_id'], 't-e')
        self.assertEqual(q['last_status'], 200)

    def test_seen_at_is_iso_string_the_frontend_can_parse(self):
        _quota('t-e', day=1, seen_at=NOW)
        body = self.api.get(self.url).json()
        seen = body['quota']['seen_at']
        self.assertIsInstance(seen, str)
        self.assertEqual(seen, '2026-08-19T12:00:00+00:00')
        # Round-trips to the same instant (what JS `new Date()` needs).
        self.assertEqual(datetime.datetime.fromisoformat(seen), NOW)

    def test_seen_at_null_when_no_row(self):
        body = self.api.get(self.url).json()
        self.assertIsNone(body['quota']['seen_at'])
        self.assertIsNone(body['quota']['day_remaining'])
        self.assertEqual(body['total_today'], 0)

    def test_tenant_id_query_param_selects_tenant(self):
        _quota('t-1', day=100, seen_at=NOW + datetime.timedelta(hours=1))
        _quota('t-2', day=200, seen_at=NOW)
        body = self.api.get(self.url, {'tenant_id': 't-2'}).json()
        self.assertEqual(body['quota']['tenant_id'], 't-2')
        self.assertEqual(body['quota']['day_remaining'], 200)
        body = self.api.get(self.url).json()
        self.assertEqual(body['quota']['tenant_id'], 't-1')

    def test_unknown_tenant_id_query_param_gives_nulls_not_other_tenant(self):
        _quota('t-1', day=100)
        body = self.api.get(self.url, {'tenant_id': 'does-not-exist'}).json()
        self.assertIsNone(body['quota']['tenant_id'])
        self.assertIsNone(body['quota']['day_remaining'])
        self.assertIsNone(body['quota']['used_estimate'])

    @override_settings(XERO_DAILY_CALL_CAP=250)
    def test_endpoint_cap_follows_setting(self):
        _quota('t-1', day=200)
        body = self.api.get(self.url).json()
        self.assertEqual(body['cap'], 250)
        self.assertEqual(body['quota']['cap'], 250)
        self.assertEqual(body['quota']['used_estimate'], 50)

    def test_post_not_allowed(self):
        self.assertEqual(self.api.post(self.url, {}).status_code, 405)
