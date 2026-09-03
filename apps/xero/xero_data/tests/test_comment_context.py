"""
A point CARRIES the transaction it is about — so a preparer can see it without
being handed the general ledger.

This is the pilot's last blocker. A bookkeeper signs in with the `auditor`
role; the live drill lives under ``/xero/data/journals/pivot/drill/`` and that
whole prefix is 403 for her. So she could be named, assigned and mailed about a
figure she could never look at — and the transaction plus its receipt was
identified as the single thing that would actually make her day easier.

The alternative was granting her the GL. That is a much larger decision than a
pilot needs and it cannot be undone quietly. Capturing the evidence onto the
point instead gets her what she needs and widens her access by nothing.

What is pinned here:

* capture stores the lines AND the receipt link, because "which transactions"
  is half an answer if the document cannot be opened from the same row;
* the value is frozen at capture, which is what later makes "it was X, it is
  now Y" answerable without anyone typing either number;
* the auditor can READ a capture and cannot CREATE one — capturing runs a
  ledger query, and the gate exists so an outside party cannot make this
  server work against the books;
* the captured lines never enter the register list, which was 1.4 MB for 113
  rows once and hung the page.
"""
import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.xero.xero_data import cube_mentions, pivot_comments

User = get_user_model()

COMMENTS = '/xero/data/journals/pivot/comments/'
CONTEXT = '/xero/data/journals/pivot/comments/%s/context/'
AUDIT_CONTEXT = '/audit/cube-comments/%s/context/'


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.xero.xero_core.models import XeroTenant
        from apps.xero.xero_data.models import (
            XeroDocument, XeroJournals, XeroTransactionSource,
        )
        from apps.xero.xero_metadata.models import XeroAccount

        cls.tenant = XeroTenant.objects.create(
            tenant_id=str(1_700_000_000 + uuid.uuid4().int % 100_000_000),
            tenant_name='Context Tenant',
        )
        cls.account = XeroAccount.objects.create(
            organisation=cls.tenant, account_id=f'acc-{uuid.uuid4()}',
            code='406', name='Consulting', type='EXPENSE', grouping='EXPENSE',
        )
        guid = str(uuid.uuid4())
        cls.ts = XeroTransactionSource.objects.create(
            organisation=cls.tenant, transactions_id=guid,
            transaction_source='BankTransaction', collection={},
        )
        # Two lines on the same account, one of them carrying a receipt.
        for n, amount in ((910001, '1200.00'), (910002, '800.00')):
            XeroJournals.objects.create(
                organisation=cls.tenant, journal_id=f'jrn-{uuid.uuid4()}',
                # NOT journal_type='journal': that is the frozen legacy mirror,
                # which the drill excludes by default (pivot_views: it would
                # double-count against the live feeds). A fixture using it
                # returns zero lines and looks like the drill is broken.
                journal_number=n, journal_type='transaction', account=cls.account,
                transaction_source=cls.ts,
                date=datetime(2026, 2, 11, tzinfo=dt_timezone.utc),
                description='Consulting fee', amount=Decimal(amount),
                debit=Decimal(amount), credit=Decimal('0'), tax_amount=Decimal('0'),
            )
        cls.doc = XeroDocument.objects.create(
            organisation=cls.tenant, transaction_source=cls.ts,
            file_name='engen-slip.pdf', content_type='application/pdf',
        )

    def setUp(self):
        self.client = APIClient()
        self.mc = User.objects.create_user(username='mc', email='mc@tremly.com',
                                           password='pw-not-logged')
        self.anzelle = User.objects.create_user(username='anzelle', password='pw',
                                                role='auditor')
        cube_mentions._ready = False
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        cube_mentions.ensure_tables()
        self.client.force_authenticate(self.mc)

    def as_auditor(self):
        """A REAL Bearer token, not force_authenticate.

        AuditorGateMiddleware resolves the caller itself, before DRF runs, so a
        force-authenticated client walks straight past the gate — the test
        would pass while asserting nothing about the thing it names.
        """
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION='Bearer %s' % RefreshToken.for_user(self.anzelle).access_token)
        return client

    def _raise_point(self, **over):
        body = {
            'measure': 'amount',
            'row_dims': ['account'],
            'row_path': ['406 — Consulting'],
            'col_dims': [],
            'col_path': '',
            'filters': {'tenant': self.tenant.tenant_id},
            'cell_value': 2000,
            'comment': 'Please recode this to 6420.',
        }
        body.update(over)
        r = self.client.post(COMMENTS, body, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data['id']


class CaptureTests(_Base):
    def test_capture_stores_the_lines_behind_the_figure(self):
        cid = self._raise_point()
        r = self.client.post(CONTEXT % cid, {}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['line_count'], 2)
        self.assertEqual(sorted(l['journal_number'] for l in r.data['lines']),
                         [910001, 910002])
        self.assertEqual(r.data['line_total'], 2000.0)

    def test_the_receipt_is_captured_with_the_line(self):
        # "Which transactions make up this number" is half an answer if the
        # document cannot be opened from the same row.
        cid = self._raise_point()
        r = self.client.post(CONTEXT % cid, {}, format='json')
        self.assertEqual(r.data['receipt_count'], 2)
        line = r.data['lines'][0]
        self.assertEqual(line['receipt_name'], 'engen-slip.pdf')
        self.assertIn('/xero/data/documents/', line['receipt_url'])
        self.assertIn('s=', line['receipt_url'], 'the link is not signed')

    def test_the_value_is_frozen_at_capture(self):
        # What later makes "it was 2,000.00, it is now 1,200.00" answerable
        # without anyone typing either number.
        cid = self._raise_point()
        r = self.client.post(CONTEXT % cid, {}, format='json')
        self.assertEqual(r.data['cell_value_at_capture'], 2000.0)

    def test_recapturing_replaces_rather_than_accumulating(self):
        cid = self._raise_point()
        first = self.client.post(CONTEXT % cid, {}, format='json').data['captured_at']
        again = self.client.post(CONTEXT % cid, {}, format='json')
        self.assertEqual(again.data['line_count'], 2, 'a re-capture doubled the lines')
        self.assertNotEqual(again.data['captured_at'], first)

    def test_a_non_cube_subject_is_refused_with_a_reason(self):
        # A bank transaction IS the transaction; storing an empty capture would
        # read as "no lines found" and look like the ledger had moved.
        r = self.client.post('/xero/data/comments/', {
            'subject_type': 'bank_txn', 'subject_key': 'txn-1',
            'comment': 'recode please',
        }, format='json')
        got = self.client.post(CONTEXT % r.data['id'], {}, format='json')
        self.assertEqual(got.status_code, 400, got.data)
        self.assertIn('bank_txn', str(got.data))

    def test_capture_for_an_unknown_comment_is_404(self):
        self.assertEqual(self.client.post(CONTEXT % 999999, {}, format='json').status_code,
                         404)


class TheRegisterStaysLightTests(_Base):
    def test_captured_lines_never_enter_the_register_list(self):
        # The list was 1.4 MB for 113 rows once and hung the page. Captures are
        # the largest thing attached to a comment and must not ride along.
        cid = self._raise_point()
        self.client.post(CONTEXT % cid, {}, format='json')
        rows = pivot_comments.list_comments({'status': 'all'})
        self.assertEqual(len(rows), 1)
        for banned in ('lines', 'context', 'line_count', 'receipt_count'):
            self.assertNotIn(banned, rows[0], 'the capture leaked into the list row')


class ThePreparerCanReadButNotCaptureTests(_Base):
    """The whole access argument, pinned."""

    def test_the_auditor_can_read_a_captured_context(self):
        cid = self._raise_point()
        self.client.post(CONTEXT % cid, {}, format='json')
        r = self.as_auditor().get(AUDIT_CONTEXT % cid)
        self.assertEqual(r.status_code, 200, getattr(r, 'data', r))
        self.assertEqual(r.data['line_count'], 2)
        self.assertEqual(r.data['lines'][0]['receipt_name'], 'engen-slip.pdf')

    def test_the_auditor_still_cannot_reach_the_live_drill(self):
        # The reason the capture exists. If this ever passes, the capture is
        # solving a problem that no longer exists and the gate has a hole.
        r = self.as_auditor().get('/xero/data/journals/pivot/drill/', {'coords': '{}'})
        self.assertEqual(r.status_code, 403)

    def test_the_auditor_cannot_capture(self):
        # Capturing runs a ledger query. An outside party must not be able to
        # make this server work against the books.
        cid = self._raise_point()
        self.assertEqual(
            self.as_auditor().post(CONTEXT % cid, {}, format='json').status_code, 403)

    def test_an_uncaptured_point_reads_as_empty_not_missing(self):
        # A preparer seeing 404 would reasonably think the point was withdrawn.
        cid = self._raise_point()
        r = self.as_auditor().get(AUDIT_CONTEXT % cid)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data['captured_at'])
        self.assertEqual(r.data['lines'], [])

    def test_both_doors_require_authentication(self):
        anon = APIClient()
        self.assertEqual(anon.get(AUDIT_CONTEXT % 1).status_code, 401)
        self.assertEqual(anon.post(CONTEXT % 1, {}, format='json').status_code, 401)
