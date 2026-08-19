"""
Adversarial tests for XeroApiQuota + XeroApiClient._record_limit_headers /
_persist_quota (the allowance-header telemetry).

ZERO network: the SDK's RESTClientObject.request is patched at the class level
for the lifetime of each test, so the real request_with_guards wrapper runs
against hand-built urllib3 responses / HTTPStatusExceptions and never opens a
socket. Time is controlled by swapping the `timezone` name inside
apps.xero.xero_core.services for a stub -- no sleep() anywhere.

Usage:
    python manage.py test apps.xero.xero_core.test_api_quota -v 2

NOTE: this lives as test_api_quota.py (not tests/test_api_quota.py) because a
`tests/` package next to the existing apps/xero/xero_core/tests.py would shadow
that module and silently drop its test cases from discovery.
"""
import datetime
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import ForeignKey
from django.test import TestCase
from django.utils import timezone
from urllib3.response import HTTPResponse
from xero_python.rest import RESTClientObject, RESTResponse
from xero_python.exceptions import HTTPStatusException

from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_core.models import XeroApiQuota, XeroTenant
from apps.xero.xero_core.services import (
    QUOTA_PERSIST_INTERVAL_SECONDS,
    QUOTA_PERSIST_NEAR_EXHAUSTION,
    XeroApiClient,
)

User = get_user_model()

T0 = datetime.datetime(2026, 8, 19, 12, 0, 0, tzinfo=datetime.timezone.utc)
TENANT = 'tenant-quota-test'


def _rest_response(status=200, headers=None, body=b'{}'):
    """A RESTResponse exactly as xero_python.rest.RESTClientObject.request returns it."""
    raw = HTTPResponse(body=body, status=status, headers=headers or {}, preload_content=True)
    return RESTResponse(raw)


def _http_error(status, headers=None):
    """The base HTTPStatusException rest.py raises for any non-2xx."""
    return HTTPStatusException(http_resp=_rest_response(status=status, headers=headers))


class _Clock:
    """Stand-in for django.utils.timezone inside services.py. Only .now() is used."""

    def __init__(self, start):
        self.current = start

    def now(self):
        return self.current

    def advance(self, seconds):
        self.current = self.current + datetime.timedelta(seconds=seconds)


class QuotaGuardTestBase(TestCase):
    """Builds a real XeroApiClient with a fake transport and a frozen clock."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='quota-user', password='x')
        XeroClientCredentials.objects.create(
            user=cls.user, client_id='cid', client_secret='sec', scope=['a'], active=True,
        )

    def setUp(self):
        self.clock = _Clock(T0)
        p = patch('apps.xero.xero_core.services.timezone', new=self.clock)
        p.start()
        self.addCleanup(p.stop)

        # Queue of responses (RESTResponse) or exceptions (HTTPStatusException)
        # the fake transport hands back, in order.
        self.script = []
        self.transport_calls = 0

        def fake_request(rest_self, *args, **kwargs):
            self.transport_calls += 1
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        p2 = patch.object(RESTClientObject, 'request', new=fake_request)
        p2.start()
        self.addCleanup(p2.stop)

        # tenant_id=None skips the token / reauth path entirely (no DB token
        # needed); the guard only needs .tenant_id set on the instance.
        self.client_ = XeroApiClient(self.user, tenant_id=None)
        self.client_.tenant_id = TENANT

    # -- helpers ---------------------------------------------------------
    def call(self, item):
        """Push one scripted response through the REAL request_with_guards wrapper."""
        self.script.append(item)
        return self.client_.api_client.rest_client.request('GET', 'https://api.xero.com/fake')

    def call_ok(self, day=None, minute=None, status=200, extra=None):
        headers = dict(extra or {})
        if day is not None:
            headers['X-DayLimit-Remaining'] = str(day)
        if minute is not None:
            headers['X-MinLimit-Remaining'] = str(minute)
        return self.call(_rest_response(status=status, headers=headers))

    def row(self):
        return XeroApiQuota.objects.filter(tenant_id=TENANT).first()


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
class XeroApiQuotaModelTests(TestCase):

    def test_tenant_id_is_plain_charfield_pk_not_fk(self):
        field = XeroApiQuota._meta.get_field('tenant_id')
        self.assertTrue(field.primary_key)
        self.assertNotIsInstance(field, ForeignKey)
        self.assertEqual(field.max_length, 100)

    def test_row_can_exist_for_tenant_with_no_xerotenant(self):
        # The guard must be able to record headers before/without a tenant row.
        self.assertFalse(XeroTenant.objects.filter(tenant_id='ghost').exists())
        XeroApiQuota.objects.create(tenant_id='ghost', day_remaining=5, seen_at=timezone.now())
        self.assertEqual(XeroApiQuota.objects.filter(tenant_id='ghost').count(), 1)

    def test_upsert_is_one_row_per_tenant(self):
        for v in (900, 800):
            XeroApiQuota.objects.update_or_create(
                tenant_id='t', defaults={'day_remaining': v, 'seen_at': timezone.now()},
            )
        self.assertEqual(XeroApiQuota.objects.filter(tenant_id='t').count(), 1)
        self.assertEqual(XeroApiQuota.objects.get(tenant_id='t').day_remaining, 800)

    def test_seen_at_is_required(self):
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                XeroApiQuota.objects.create(tenant_id='t2', day_remaining=1)


# ---------------------------------------------------------------------------
# Guard: first write / header parsing
# ---------------------------------------------------------------------------
class QuotaHeaderParsingTests(QuotaGuardTestBase):

    def test_first_response_writes_row_with_canonical_headers(self):
        self.call_ok(day=946, minute=58)
        row = self.row()
        self.assertIsNotNone(row)
        self.assertEqual(row.day_remaining, 946)
        self.assertEqual(row.min_remaining, 58)
        self.assertEqual(row.last_status, 200)
        self.assertEqual(row.seen_at, T0)
        self.assertEqual(self.client_.day_limit_remaining, 946)
        self.assertEqual(self.client_.min_limit_remaining, 58)
        self.assertEqual(self.client_._quota_last_written_at, T0)
        self.assertEqual(self.transport_calls, 1)

    def test_lowercase_header_names_parsed(self):
        self.call(_rest_response(headers={
            'x-daylimit-remaining': '123', 'x-minlimit-remaining': '45',
        }))
        self.assertEqual(self.client_.day_limit_remaining, 123)
        self.assertEqual(self.client_.min_limit_remaining, 45)
        self.assertEqual(self.row().day_remaining, 123)
        self.assertEqual(self.row().min_remaining, 45)

    def test_lowercase_dict_source_parsed_directly(self):
        self.client_._record_limit_headers({'x-daylimit-remaining': '7', 'x-minlimit-remaining': '3'})
        self.assertEqual(self.client_.day_limit_remaining, 7)
        self.assertEqual(self.client_.min_limit_remaining, 3)
        self.assertEqual(self.row().day_remaining, 7)
        # A bare dict has no .status -> last_status must be None, not garbage.
        self.assertIsNone(self.row().last_status)

    def test_zero_day_remaining_header_is_not_dropped(self):
        """'0' is the value that matters most (budget exhausted) and is falsy-ish."""
        self.call_ok(day=0, minute=0)
        self.assertEqual(self.client_.day_limit_remaining, 0)
        self.assertEqual(self.client_.min_limit_remaining, 0)
        row = self.row()
        self.assertIsNotNone(row, 'day=0 must be persisted')
        self.assertEqual(row.day_remaining, 0)
        self.assertEqual(row.min_remaining, 0)

    def test_zero_day_remaining_overwrites_previous_nonzero_value(self):
        self.call_ok(day=500, minute=50)
        self.clock.advance(QUOTA_PERSIST_INTERVAL_SECONDS)
        self.call_ok(day=0, minute=0)
        self.assertEqual(self.row().day_remaining, 0)
        self.assertEqual(self.row().min_remaining, 0)
        self.assertEqual(self.client_.day_limit_remaining, 0)

    def test_int_zero_in_dict_source_is_not_dropped_by_truthiness(self):
        """FIXED 2026-08-20: `headers.get('x-...') or headers.get('X-...')` treats an
        int 0 as missing. Real urllib3 headers are strings ('0' is truthy) so
        the HTTP path is safe, but _headers_from() explicitly accepts a dict
        source and a dict carrying int 0 loses the exhausted-budget signal.
        """
        self.client_._record_limit_headers({'x-daylimit-remaining': 0, 'x-minlimit-remaining': 0})
        self.assertEqual(self.client_.day_limit_remaining, 0)
        self.assertEqual(self.client_.min_limit_remaining, 0)

    def test_missing_headers_no_write_no_raise(self):
        resp = self.call_ok()  # no allowance headers at all
        self.assertIsNotNone(resp)
        self.assertIsNone(self.client_.day_limit_remaining)
        self.assertIsNone(self.client_.min_limit_remaining)
        self.assertIsNone(self.row())
        self.assertIsNone(self.client_._quota_last_written_at)

    def test_none_source_no_raise(self):
        self.client_._record_limit_headers(None)
        self.assertIsNone(self.row())

    def test_garbage_day_header_does_not_raise_and_does_not_write(self):
        for garbage in ('abc', '', '12.5', ' ', 'NaN', '1e3'):
            with self.subTest(garbage=repr(garbage)):
                resp = self.call(_rest_response(headers={'X-DayLimit-Remaining': garbage}))
                self.assertIsNotNone(resp)
        self.assertIsNone(self.client_.day_limit_remaining)
        self.assertIsNone(self.row())

    def test_garbage_minute_header_does_not_raise(self):
        self.call(_rest_response(headers={
            'X-DayLimit-Remaining': '800', 'X-MinLimit-Remaining': 'lots',
        }))
        self.assertIsNone(self.client_.min_limit_remaining)

    def test_garbage_day_header_does_not_discard_valid_minute_header(self):
        """FIXED 2026-08-20: a ValueError on the day header returns before the minute
        header is parsed or anything is persisted, so one bad header discards
        the other good one. Not a production risk (Xero sends ints) but the
        parse is all-or-nothing where it should be per-header.
        """
        self.call(_rest_response(headers={
            'X-DayLimit-Remaining': 'abc', 'X-MinLimit-Remaining': '55',
        }))
        self.assertEqual(self.client_.min_limit_remaining, 55)
        self.assertIsNotNone(self.row())
        self.assertEqual(self.row().min_remaining, 55)

    def test_negative_values_parse_and_persist_without_raise(self):
        self.call_ok(day=-5, minute=-1)
        self.assertEqual(self.client_.day_limit_remaining, -5)
        self.assertEqual(self.row().day_remaining, -5)

    def test_whitespace_padded_int_parses(self):
        self.call(_rest_response(headers={'X-DayLimit-Remaining': ' 42 '}))
        self.assertEqual(self.client_.day_limit_remaining, 42)

    def test_minute_only_header_persists_cached_day_value(self):
        self.call_ok(day=700, minute=60)
        self.clock.advance(QUOTA_PERSIST_INTERVAL_SECONDS)
        self.call_ok(minute=59)  # no day header this time
        row = self.row()
        self.assertEqual(row.min_remaining, 59)
        self.assertEqual(row.day_remaining, 700, 'cached day value must be kept, not nulled')
        self.assertEqual(row.seen_at, self.clock.current)

    def test_headers_as_list_of_tuples_does_not_raise(self):
        """FIXED 2026-08-20 -- must never raise: _headers_from() returns whatever .headers is, and
        _record_limit_headers only catches (TypeError, ValueError). A source
        whose .headers is a list of 2-tuples (http.client style) raises
        AttributeError on .get() and that escapes the guard -- which would
        abort the API call it rides on. Low likelihood with urllib3, but the
        contract says never.
        """
        src = SimpleNamespace(headers=[('X-DayLimit-Remaining', '5')], status=200)
        self.client_._record_limit_headers(src)  # must not raise


# ---------------------------------------------------------------------------
# Guard: throttle + bypass
# ---------------------------------------------------------------------------
class QuotaThrottleTests(QuotaGuardTestBase):

    def _first_write(self, day=900, minute=60):
        self.call_ok(day=day, minute=minute)
        row = self.row()
        self.assertEqual(row.seen_at, T0)
        return row

    def test_second_response_one_second_later_does_not_write(self):
        self._first_write()
        self.clock.advance(1)
        self.call_ok(day=899, minute=59)
        row = self.row()
        # Row must be byte-for-byte unchanged ...
        self.assertEqual(row.seen_at, T0)
        self.assertEqual(row.day_remaining, 900)
        self.assertEqual(row.min_remaining, 60)
        self.assertEqual(self.client_._quota_last_written_at, T0)
        # ... but the in-memory telemetry must still track the latest response.
        self.assertEqual(self.client_.day_limit_remaining, 899)
        self.assertEqual(self.client_.min_limit_remaining, 59)

    def test_just_under_interval_does_not_write(self):
        self._first_write()
        self.clock.advance(QUOTA_PERSIST_INTERVAL_SECONDS - 0.001)
        self.call_ok(day=899, minute=59)
        self.assertEqual(self.row().seen_at, T0)
        self.assertEqual(self.row().day_remaining, 900)

    def test_exactly_interval_writes(self):
        self._first_write()
        self.clock.advance(QUOTA_PERSIST_INTERVAL_SECONDS)
        self.call_ok(day=899, minute=59)
        row = self.row()
        self.assertEqual(row.seen_at, self.clock.current)
        self.assertEqual(row.day_remaining, 899)
        self.assertEqual(row.min_remaining, 59)
        self.assertEqual(self.client_._quota_last_written_at, self.clock.current)

    def test_after_interval_writes(self):
        self._first_write()
        self.clock.advance(QUOTA_PERSIST_INTERVAL_SECONDS + 30)
        self.call_ok(day=850, minute=10)
        self.assertEqual(self.row().day_remaining, 850)
        self.assertEqual(self.row().seen_at, self.clock.current)

    def test_burst_of_many_responses_inside_window_writes_once(self):
        self._first_write()
        for i in range(50):
            self.clock.advance(0.1)
            self.call_ok(day=900 - i - 1, minute=60)
        self.assertEqual(self.row().seen_at, T0)
        self.assertEqual(self.row().day_remaining, 900)
        self.assertEqual(self.transport_calls, 51)

    def test_throttle_window_restarts_from_last_write_not_first(self):
        self._first_write()
        self.clock.advance(QUOTA_PERSIST_INTERVAL_SECONDS)
        self.call_ok(day=880, minute=60)
        t1 = self.clock.current
        self.assertEqual(self.row().seen_at, t1)
        self.clock.advance(QUOTA_PERSIST_INTERVAL_SECONDS - 1)
        self.call_ok(day=870, minute=60)
        self.assertEqual(self.row().seen_at, t1, 'window must be measured from the last write')
        self.assertEqual(self.row().day_remaining, 880)

    def test_bypass_day_remaining_at_threshold_writes_inside_window(self):
        self._first_write(day=200)
        self.clock.advance(1)
        self.call_ok(day=QUOTA_PERSIST_NEAR_EXHAUSTION, minute=60)  # == 100
        row = self.row()
        self.assertEqual(row.day_remaining, QUOTA_PERSIST_NEAR_EXHAUSTION)
        self.assertEqual(row.seen_at, self.clock.current)

    def test_day_remaining_one_above_threshold_does_not_bypass(self):
        self._first_write(day=200)
        self.clock.advance(1)
        self.call_ok(day=QUOTA_PERSIST_NEAR_EXHAUSTION + 1, minute=60)  # == 101
        row = self.row()
        self.assertEqual(row.day_remaining, 200)
        self.assertEqual(row.seen_at, T0)

    def test_bypass_every_response_once_near_exhaustion(self):
        self._first_write(day=100)
        for day in (99, 98, 97):
            self.clock.advance(0.2)
            self.call_ok(day=day, minute=60)
            self.assertEqual(self.row().day_remaining, day)
            self.assertEqual(self.row().seen_at, self.clock.current)

    def test_bypass_day_zero_inside_window(self):
        self._first_write(day=500)
        self.clock.advance(1)
        self.call_ok(day=0, minute=0)
        self.assertEqual(self.row().day_remaining, 0)
        self.assertEqual(self.row().seen_at, self.clock.current)

    def test_bypass_on_429_exception_inside_window_and_still_reraises(self):
        self._first_write(day=900)
        self.clock.advance(1)
        with self.assertRaises(HTTPStatusException):
            self.call(_http_error(429, headers={
                'X-DayLimit-Remaining': '890', 'X-MinLimit-Remaining': '0',
            }))
        row = self.row()
        self.assertEqual(row.last_status, 429)
        self.assertEqual(row.day_remaining, 890)
        self.assertEqual(row.min_remaining, 0)
        self.assertEqual(row.seen_at, self.clock.current)

    def test_bypass_on_unraised_429_response_inside_window(self):
        self._first_write(day=900)
        self.clock.advance(1)
        # Backstop branch: a 429 RESTResponse returned rather than raised.
        resp = self.call_ok(day=888, minute=0, status=429)
        self.assertIsNotNone(resp)
        self.assertEqual(self.row().last_status, 429)
        self.assertEqual(self.row().day_remaining, 888)

    def test_non_429_error_inside_window_does_not_bypass(self):
        self._first_write(day=900)
        self.clock.advance(1)
        with self.assertRaises(HTTPStatusException):
            self.call(_http_error(500, headers={'X-DayLimit-Remaining': '899'}))
        self.assertEqual(self.row().day_remaining, 900)
        self.assertEqual(self.row().seen_at, T0)
        self.assertEqual(self.row().last_status, 200)
        # in-memory still updated from the error response
        self.assertEqual(self.client_.day_limit_remaining, 899)

    def test_error_response_last_status_recorded_on_first_write(self):
        with self.assertRaises(HTTPStatusException):
            self.call(_http_error(401, headers={'X-DayLimit-Remaining': '950'}))
        self.assertEqual(self.row().last_status, 401)
        self.assertEqual(self.row().day_remaining, 950)

    def test_throttle_state_is_per_client_instance(self):
        self._first_write(day=900)
        self.clock.advance(1)
        other = XeroApiClient(self.user, tenant_id=None)
        other.tenant_id = TENANT
        other._record_limit_headers(_rest_response(headers={'X-DayLimit-Remaining': '899'}))
        self.assertEqual(self.row().day_remaining, 899, 'a fresh client has nothing written yet')
        self.assertEqual(self.row().seen_at, self.clock.current)

    def test_no_tenant_id_never_writes(self):
        self.client_.tenant_id = None
        self.call_ok(day=5, minute=5)
        self.assertEqual(XeroApiQuota.objects.count(), 0)
        self.assertEqual(self.client_.day_limit_remaining, 5)
        self.client_.tenant_id = ''
        self.call_ok(day=4, minute=4)
        self.assertEqual(XeroApiQuota.objects.count(), 0)


# ---------------------------------------------------------------------------
# Guard: never raises / never poisons the caller's transaction
# ---------------------------------------------------------------------------
class QuotaPersistFailureTests(QuotaGuardTestBase):

    TOO_LONG = 'x' * 101  # > XeroApiQuota.tenant_id max_length=100 -> DataError

    def test_db_error_inside_outer_atomic_does_not_raise_or_poison(self):
        self.client_.tenant_id = self.TOO_LONG
        with transaction.atomic():
            # Simulate a caller who opened a transaction before the API call.
            # assertLogs proves the DB error really happened (Postgres rejected
            # the 101-char PK) rather than the write silently succeeding.
            with self.assertLogs('apps.xero.xero_core.services', level='DEBUG') as logs:
                resp = self.call_ok(day=10, minute=10)  # persist fails inside
            self.assertTrue(any('persist skipped' in m and 'too long' in m for m in logs.output), logs.output)
            self.assertIsNotNone(resp)
            # The outer transaction must still be usable: real write, real read.
            XeroTenant.objects.create(tenant_id='after-failure', tenant_name='ok')
            self.assertTrue(XeroTenant.objects.filter(tenant_id='after-failure').exists())
        # Committed out of the caller's block (TestCase wraps it, but a poisoned
        # connection would have raised TransactionManagementError above).
        self.assertTrue(XeroTenant.objects.filter(tenant_id='after-failure').exists())
        self.assertEqual(XeroApiQuota.objects.count(), 0)
        self.assertIsNone(self.client_._quota_last_written_at)
        # In-memory telemetry still recorded despite the persist failure.
        self.assertEqual(self.client_.day_limit_remaining, 10)

    def test_db_error_in_autocommit_does_not_raise(self):
        self.client_.tenant_id = self.TOO_LONG
        resp = self.call_ok(day=10, minute=10)
        self.assertIsNotNone(resp)
        self.assertEqual(XeroApiQuota.objects.count(), 0)
        # Connection still fine afterwards.
        XeroTenant.objects.create(tenant_id='after-failure-2', tenant_name='ok')

    def test_failure_does_not_arm_throttle_so_next_good_call_writes(self):
        self.client_.tenant_id = self.TOO_LONG
        self.call_ok(day=10, minute=10)
        self.assertIsNone(self.client_._quota_last_written_at)
        self.client_.tenant_id = TENANT
        self.clock.advance(1)  # well inside the window -- only OK because nothing was written
        self.call_ok(day=9, minute=9)
        self.assertEqual(self.row().day_remaining, 9)

    def test_orm_exception_is_swallowed_and_api_response_returned(self):
        with patch('apps.xero.xero_core.services.XeroApiQuota.objects.update_or_create',
                   side_effect=RuntimeError('boom')):
            resp = self.call_ok(day=10, minute=10)
        self.assertIsNotNone(resp)
        self.assertEqual(self.client_.day_limit_remaining, 10)
        self.assertIsNone(self.client_._quota_last_written_at)

    def test_429_with_failed_persist_still_raises_the_429_not_the_db_error(self):
        self.client_.tenant_id = self.TOO_LONG
        with self.assertRaises(HTTPStatusException):
            self.call(_http_error(429, headers={'X-DayLimit-Remaining': '5'}))
