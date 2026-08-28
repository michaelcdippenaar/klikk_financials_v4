"""Guard rails on the aged-report sweep.

Written after 28 Aug 2026, when one Standard sync reported `succeeded` while
recording 274 errors and writing no rows, and spent roughly 83% of the Klikk
tenant's daily Xero allowance doing it.

Three defects combined:
  1. the sweep slept 1.1s after every 50 calls — a ~2,700/min burst against a
     60/min limit, so it tripped the per-minute window almost immediately;
  2. a minute-window 429 was caught as a generic exception, counted as a
     per-contact error, and skipped past, so it kept issuing calls that could
     not succeed until the contact list ran out;
  3. these were the only two stages whose result was never checked, so the run
     reported success regardless.

Each is pinned below.
"""
from datetime import date
from unittest.mock import patch

from django.test import TestCase
from xero_python.exceptions import RateLimitException

from apps.xero.xero_core.exceptions import DailyLimitReached
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.models import AgedPayable
from apps.xero.xero_metadata.models import XeroContacts


class _Budget:
    """Stands in for XeroApiClient, which is the only carrier of the day budget."""

    def __init__(self, day_limit_remaining=None):
        self.day_limit_remaining = day_limit_remaining


def _report(total='100.00'):
    return {'Reports': [{'Rows': [{
        'RowType': 'SummaryRow',
        'Cells': [{'Value': 'Total'}, {'Value': '10.00'}, {'Value': '20.00'},
                  {'Value': '30.00'}, {'Value': '40.00'}, {'Value': '0.00'},
                  {'Value': total}],
    }]}]}


class _Resp:
    """Xero reads rate_limit and Retry-After off the response headers, so a
    realistic 429 has to carry them rather than pass them as arguments."""

    def __init__(self, problem, retry_after):
        self.status = 429
        self.reason = 'Too Many Requests'
        self.data = b''
        self._headers = {
            'x-rate-limit-problem': problem,
            'Retry-After': retry_after,
        }

    def getheaders(self):
        return self._headers


def _rate_limited(problem='minute', retry_after='5'):
    return RateLimitException(http_resp=_Resp(problem, retry_after))


class AgedSweepGuardTests(TestCase):
    def setUp(self):
        self.tenant = XeroTenant.objects.create(
            tenant_id='tenant-aged', tenant_name='Aged Co',
        )
        for index in range(5):
            XeroContacts.objects.create(
                organisation=self.tenant,
                contacts_id=f'contact-{index}',
                name=f'Supplier {index}',
                collection={'IsSupplier': True},
            )

    def _run(self, side_effect, budget=None, **kwargs):
        from apps.xero.xero_data import aged_reports_service as service

        class _Api:
            def get_report_aged_payables_by_contact(self, *args, **kw):
                return side_effect()

        with patch.object(service, '_get_api', return_value=(_Api(), budget or _Budget())), \
                patch.object(service, 'serialize_model', side_effect=lambda value: value), \
                patch('apps.xero.xero_core.throttle.time.sleep'):
            return service.sync_aged_payables(
                self.tenant, report_date=date(2026, 8, 31), **kwargs,
            )

    def test_minute_window_429_is_retried_not_counted_as_a_contact_error(self):
        """The defect: a per-minute 429 became errors += 1 and moved on."""
        calls = {'n': 0}

        def flaky():
            calls['n'] += 1
            # Fail the first attempt of the first contact only.
            if calls['n'] == 1:
                raise _rate_limited()
            return _report()

        stats = self._run(flaky)

        self.assertEqual(stats['errors'], 0, stats)
        self.assertEqual(stats['created'], 5)
        # 5 contacts plus the one retried attempt.
        self.assertEqual(stats['api_calls'], 6)

    def test_a_day_window_429_stops_the_sweep_instead_of_walking_every_contact(self):
        def dead():
            raise DailyLimitReached('day limit')

        stats = self._run(dead)

        self.assertEqual(stats['stopped_early'], 'daily-limit')
        # One call proves it stopped rather than calling all five contacts.
        self.assertEqual(stats['api_calls'], 1)
        self.assertEqual(AgedPayable.objects.count(), 0)

    def test_the_sweep_stops_at_its_api_call_budget(self):
        stats = self._run(_report, max_api_calls=2)

        self.assertEqual(stats['stopped_early'], 'max-api-calls')
        self.assertEqual(stats['api_calls'], 2)
        self.assertEqual(stats['contact_count'], 5)
        self.assertEqual(stats['contacts_processed'], 2)

    def test_the_sweep_stops_when_the_daily_allowance_nears_its_floor(self):
        # The nightly pipeline must never be starved by an interactive click.
        stats = self._run(_report, budget=_Budget(day_limit_remaining=10), headroom=300)

        self.assertEqual(stats['stopped_early'], 'headroom-floor')
        # The floor is checked before the call is made, so nothing is spent.
        self.assertEqual(stats['api_calls'], 0)

    def test_genuine_per_contact_errors_are_still_counted(self):
        def broken():
            raise ValueError('malformed payload')

        stats = self._run(broken)

        self.assertEqual(stats['errors'], 5)
        self.assertEqual(stats['created'], 0)
