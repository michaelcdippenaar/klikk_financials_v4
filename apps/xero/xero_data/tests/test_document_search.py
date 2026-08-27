"""
Adversarial contract tests for the Xero document search + signed file endpoints.

Contract under test (spec, NOT the implementation):

  A) GET /xero/data/documents/search/          (DRF, IsAuthenticated)
     params: invoice_number (icontains), amount (+/-0.01 vs invoice total OR
     journal debit on the same transaction source), q (icontains across
     invoice.contact_name / journal.description / document.file_name),
     date_from / date_to (YYYY-MM-DD, inclusive, vs invoice.date),
     tenant_id, limit (default 20, clamped 1..100).
     Invalid amount -> 400 {"error": "amount must be a decimal number"}.
     200 body: {"count", "limit", "results": [{id, file_name, content_type,
     invoice_number, contact_name, date, total, transaction_source,
     transaction_id, view_url}]}; invoice fields null when the transaction
     has no XeroInvoice; total is a decimal STRING; date is "YYYY-MM-DD";
     view_url is an absolute signed URL for endpoint B. Anonymous -> 401.

  B) GET /xero/data/documents/<int:document_id>/file/?s=<sig>   (public)
     sig == hmac_sha256(SECRET_KEY, str(document_id)).hexdigest()[:32]
     valid sig + stored file -> 200 exact bytes, Content-Type == stored
     content_type, Content-Disposition starts with "inline".
     missing/tampered sig -> 403. good sig for unknown id -> 404.

These tests build fixtures with production-realistic shapes (real GUIDs,
BankTransaction attachments with no invoice, journal lines with required
account FK, a second tenant) and MUST NOT be weakened to make the
implementation pass.

Usage:
    python manage.py test apps.xero.xero_data.tests.test_document_search
"""
import hashlib
import hmac
import os
import uuid
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from urllib.parse import urlsplit

from django.conf import settings
from django.core.files.base import ContentFile
from django.test import TestCase
from rest_framework.test import APIClient


SEARCH_PATH = '/xero/data/documents/search/'
FILE_PATH_TEMPLATE = '/xero/data/documents/{document_id}/file/'

# Byte-exact payload for the signed-file tests. Realistic PDF magic header.
DOC1_BYTES = b'%PDF-1.4\n% adversarial doc-search test payload 7f3a9c\n%%EOF\n'


def expected_signature(document_id):
    """Independent implementation of the contract signature. Deliberately NOT
    the helper under test — a wrong helper must not self-validate."""
    return hmac.new(
        settings.SECRET_KEY.encode(),
        str(document_id).encode(),
        hashlib.sha256,
    ).hexdigest()[:32]


def file_url(document_id, sig=None):
    url = FILE_PATH_TEMPLATE.format(document_id=document_id)
    if sig is not None:
        url += f'?s={sig}'
    return url


class DocumentSearchFixtureMixin:
    """Shared production-shaped fixture.

    NOTE: no XeroClientCredentials / XeroTenantToken rows are created anywhere
    in this module — the endpoints must run from the local DB alone with an
    absent Xero credential set (contract test 14).
    """

    _saved_files = []  # (storage, name) pairs for physical cleanup

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from apps.xero.xero_core.models import XeroTenant
        from apps.xero.xero_data.models import (
            XeroDocument, XeroInvoice, XeroJournals, XeroTransactionSource,
        )
        from apps.xero.xero_metadata.models import XeroAccount

        cls._saved_files = []

        cls.user = get_user_model().objects.create_user(
            username=f'doc-search-user-{uuid.uuid4().hex[:8]}',
            email='doc-search-tester@example.com',
            password='testpass123',
        )
        # NOTE: tenant_id is the PK and would realistically be a Xero org
        # UUID. Numeric strings were originally required here because the
        # glossary-refresh receiver stuffed the tenant pk into an IntegerField
        # and crashed every XeroAccount save; that is fixed. They are kept
        # because they are unique per run and the search contract does not care.
        cls.tenant = XeroTenant.objects.create(
            tenant_id=str(1_900_000_000 + uuid.uuid4().int % 100_000_000),
            tenant_name='Doc Search Tenant A',
        )
        cls.tenant_b = XeroTenant.objects.create(
            tenant_id=str(1_800_000_000 + uuid.uuid4().int % 100_000_000),
            tenant_name='Doc Search Tenant B',
        )

        cls.account = XeroAccount.objects.create(
            organisation=cls.tenant,
            account_id=f'acc-{uuid.uuid4()}',
            code='5000',
            name='Cost of Sales',
            type='EXPENSE',
            grouping='EXPENSE',
        )
        cls.account_b = XeroAccount.objects.create(
            organisation=cls.tenant_b,
            account_id=f'acc-{uuid.uuid4()}',
            code='4000',
            name='Sales',
            type='REVENUE',
            grouping='REVENUE',
        )

        # --- TS1: real sales invoice with a stored PDF (the DME shape) ------
        guid1 = str(uuid.uuid4())
        cls.ts1 = XeroTransactionSource.objects.create(
            organisation=cls.tenant,
            transactions_id=guid1,
            transaction_source='Invoice',
            collection={},
        )
        cls.invoice1 = XeroInvoice.objects.create(
            organisation=cls.tenant,
            invoice_id=guid1,
            invoice_number='INV-0263',
            contact_name='DME Civil Projects (Pty) Ltd',
            date=date(2025, 9, 26),
            total=Decimal('256676.23'),
            type='ACCREC',
            status='AUTHORISED',
        )
        cls.doc1 = XeroDocument.objects.create(
            organisation=cls.tenant,
            transaction_source=cls.ts1,
            file_name='INV-0263-signed.pdf',
            content_type='application/pdf',
        )
        cls.doc1.file.save('INV-0263-signed.pdf', ContentFile(DOC1_BYTES), save=True)
        cls._saved_files.append((cls.doc1.file.storage, cls.doc1.file.name))

        # --- TS2: a second, different invoice (must NOT bleed into TS1
        # searches), with a journal line on the same transaction ------------
        guid2 = str(uuid.uuid4())
        cls.ts2 = XeroTransactionSource.objects.create(
            organisation=cls.tenant,
            transactions_id=guid2,
            transaction_source='Invoice',
            collection={},
        )
        cls.invoice2 = XeroInvoice.objects.create(
            organisation=cls.tenant,
            invoice_id=guid2,
            invoice_number='INV-0264',
            contact_name='Boschendal Estates',
            date=date(2025, 10, 15),
            total=Decimal('1500.00'),
            type='ACCREC',
            status='AUTHORISED',
        )
        XeroJournals.objects.create(
            organisation=cls.tenant,
            journal_id=f'jrn-{uuid.uuid4()}',
            journal_number=900001,
            journal_type='journal',
            account=cls.account,
            transaction_source=cls.ts2,
            date=datetime(2025, 10, 15, tzinfo=dt_timezone.utc),
            description='Hire of stage equipment',
            amount=Decimal('1500.00'),
            debit=Decimal('1500.00'),
            credit=Decimal('0'),
            tax_amount=Decimal('0'),
        )
        cls.doc2 = XeroDocument.objects.create(
            organisation=cls.tenant,
            transaction_source=cls.ts2,
            file_name='INV-0264.pdf',
            content_type='application/pdf',
        )

        # --- TS3: BankTransaction attachment — NO invoice at all. This is
        # the null-shape + journal-debit-amount case. ------------------------
        guid3 = str(uuid.uuid4())
        cls.ts3 = XeroTransactionSource.objects.create(
            organisation=cls.tenant,
            transactions_id=guid3,
            transaction_source='BankTransaction',
            collection={},
        )
        XeroJournals.objects.create(
            organisation=cls.tenant,
            journal_id=f'jrn-{uuid.uuid4()}',
            journal_number=900002,
            journal_type='journal',
            account=cls.account,
            transaction_source=cls.ts3,
            date=datetime(2025, 11, 3, tzinfo=dt_timezone.utc),
            description='Diesel purchase Engen Kraaifontein',
            amount=Decimal('4321.09'),
            debit=Decimal('4321.09'),
            credit=Decimal('0'),
            tax_amount=Decimal('563.62'),
        )
        cls.doc3 = XeroDocument.objects.create(
            organisation=cls.tenant,
            transaction_source=cls.ts3,
            file_name='engen-slip-20251103.jpg',
            content_type='image/jpeg',
        )

        # --- TS4: SQL-wildcard bait — literal underscore in one file name ---
        cls.ts4 = XeroTransactionSource.objects.create(
            organisation=cls.tenant,
            transactions_id=str(uuid.uuid4()),
            transaction_source='BankTransaction',
            collection={},
        )
        cls.doc_underscore = XeroDocument.objects.create(
            organisation=cls.tenant,
            transaction_source=cls.ts4,
            file_name='WBRX_report.pdf',
            content_type='application/pdf',
        )
        cls.doc_no_underscore = XeroDocument.objects.create(
            organisation=cls.tenant,
            transaction_source=cls.ts4,
            file_name='WBRXZreport.pdf',
            content_type='application/pdf',
        )

        # --- Tenant B: one invoice + doc, to prove tenant_id filtering ------
        guid_b = str(uuid.uuid4())
        cls.ts_b = XeroTransactionSource.objects.create(
            organisation=cls.tenant_b,
            transactions_id=guid_b,
            transaction_source='Invoice',
            collection={},
        )
        cls.invoice_b = XeroInvoice.objects.create(
            organisation=cls.tenant_b,
            invoice_id=guid_b,
            invoice_number='TB-INV-0001',
            contact_name='Tenant B Holdings',
            date=date(2025, 8, 1),
            total=Decimal('999.99'),
            type='ACCPAY',
            status='AUTHORISED',
        )
        cls.doc_b = XeroDocument.objects.create(
            organisation=cls.tenant_b,
            transaction_source=cls.ts_b,
            file_name='tenantb-doc.pdf',
            content_type='application/pdf',
        )

        # --- Bulk rows so the default limit (20) actually bites -------------
        cls.ts_bulk = XeroTransactionSource.objects.create(
            organisation=cls.tenant,
            transactions_id=str(uuid.uuid4()),
            transaction_source='BankTransaction',
            collection={},
        )
        cls.bulk_docs = [
            XeroDocument.objects.create(
                organisation=cls.tenant,
                transaction_source=cls.ts_bulk,
                file_name=f'bulk-doc-{i:03d}.pdf',
                content_type='application/pdf',
            )
            for i in range(25)
        ]

    @classmethod
    def tearDownClass(cls):
        # Physical files are NOT rolled back with the test DB — remove them
        # from the real storage root and prune any now-empty test dirs.
        for storage, name in cls._saved_files:
            try:
                storage.delete(name)
                try:
                    os.removedirs(os.path.dirname(storage.path(name)))
                except OSError:
                    pass  # dir not empty / already gone — fine
            except Exception:
                pass
        super().tearDownClass()

    def authed_client(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        return client

    def search(self, **params):
        response = self.authed_client().get(SEARCH_PATH, params)
        self.assertEqual(
            response.status_code, 200,
            f'search {params!r} -> {response.status_code}: '
            f'{getattr(response, "content", b"")[:500]}',
        )
        return response.json()

    def result_ids(self, body):
        return {row['id'] for row in body['results']}


class DocumentSearchFilterTests(DocumentSearchFixtureMixin, TestCase):
    """Contract tests for GET /xero/data/documents/search/ filters + shape."""

    # (1) ------------------------------------------------------------------
    def test_search_by_invoice_number_returns_only_matching_documents(self):
        body = self.search(invoice_number='INV-0263')
        self.assertEqual(self.result_ids(body), {self.doc1.id})
        self.assertEqual(body['count'], 1)
        row = body['results'][0]
        self.assertEqual(row['file_name'], 'INV-0263-signed.pdf')
        self.assertEqual(row['invoice_number'], 'INV-0263')
        self.assertEqual(row['contact_name'], 'DME Civil Projects (Pty) Ltd')
        self.assertEqual(row['transaction_source'], 'Invoice')
        self.assertEqual(row['transaction_id'], self.ts1.transactions_id)

    def test_search_by_invoice_number_is_case_insensitive_contains(self):
        body = self.search(invoice_number='inv-0263')
        self.assertEqual(self.result_ids(body), {self.doc1.id})

    # (2) ------------------------------------------------------------------
    def test_amount_matches_invoice_total_within_tolerance(self):
        for probe in ('256676.23', '256676.24', '256676.22'):
            body = self.search(amount=probe)
            self.assertIn(
                self.doc1.id, self.result_ids(body),
                f'amount={probe} must find the 256676.23 invoice document',
            )

    def test_amount_outside_tolerance_finds_nothing(self):
        body = self.search(amount='256676.50')
        self.assertNotIn(self.doc1.id, self.result_ids(body))
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['results'], [])

    # (3) ------------------------------------------------------------------
    def test_amount_matches_journal_debit_when_no_invoice_total_matches(self):
        # TS3 is a BankTransaction: no XeroInvoice exists, so the ONLY route
        # to this document is the journal-debit leg of the amount filter.
        body = self.search(amount='4321.09')
        self.assertEqual(self.result_ids(body), {self.doc3.id})

    def test_amount_journal_debit_tolerance(self):
        body = self.search(amount='4321.10')
        self.assertIn(self.doc3.id, self.result_ids(body))

    # (4) ------------------------------------------------------------------
    def test_q_matches_invoice_contact_name(self):
        body = self.search(q='Civil Projects')
        self.assertEqual(self.result_ids(body), {self.doc1.id})

    def test_q_matches_journal_description(self):
        body = self.search(q='stage equipment')
        self.assertEqual(self.result_ids(body), {self.doc2.id})

    def test_q_matches_document_file_name(self):
        body = self.search(q='engen-slip')
        self.assertEqual(self.result_ids(body), {self.doc3.id})

    # (5) ------------------------------------------------------------------
    def test_date_bounds_are_inclusive(self):
        body = self.search(date_from='2025-09-26', date_to='2025-09-26')
        self.assertIn(self.doc1.id, self.result_ids(body))
        self.assertNotIn(self.doc2.id, self.result_ids(body))

        body = self.search(date_from='2025-10-15', date_to='2025-10-15')
        self.assertIn(self.doc2.id, self.result_ids(body))
        self.assertNotIn(self.doc1.id, self.result_ids(body))

    def test_date_bounds_exclude_out_of_range(self):
        body = self.search(date_from='2025-09-27', date_to='2025-10-14')
        ids = self.result_ids(body)
        self.assertNotIn(self.doc1.id, ids, 'date_from must exclude 2025-09-26')
        self.assertNotIn(self.doc2.id, ids, 'date_to must exclude 2025-10-15')

    # (6) ------------------------------------------------------------------
    def test_limit_defaults_to_20(self):
        body = self.search()
        self.assertEqual(body['limit'], 20)
        self.assertEqual(len(body['results']), 20,
                         'fixture has 31 documents; default limit must cap at 20')

    def test_limit_clamped_to_100_and_floor_1(self):
        body = self.search(limit='500')
        self.assertEqual(body['limit'], 100)

        body = self.search(limit='0')
        self.assertEqual(body['limit'], 1)
        self.assertLessEqual(len(body['results']), 1)

    # (7) ------------------------------------------------------------------
    def test_bank_transaction_attachment_has_null_invoice_fields(self):
        body = self.search(q='engen-slip-20251103')
        self.assertEqual(body['count'], 1)
        row = body['results'][0]
        self.assertEqual(row['id'], self.doc3.id)
        self.assertEqual(row['file_name'], 'engen-slip-20251103.jpg')
        self.assertEqual(row['content_type'], 'image/jpeg')
        self.assertIsNone(row['invoice_number'])
        self.assertIsNone(row['contact_name'])
        self.assertIsNone(row['date'])
        self.assertIsNone(row['total'])
        self.assertEqual(row['transaction_source'], 'BankTransaction')
        self.assertEqual(row['transaction_id'], self.ts3.transactions_id)

    # (8) ------------------------------------------------------------------
    def test_anonymous_request_is_401(self):
        response = APIClient().get(SEARCH_PATH)
        self.assertEqual(response.status_code, 401)

    # (9) ------------------------------------------------------------------
    def test_invalid_amount_is_400_with_contracted_body(self):
        response = self.authed_client().get(SEARCH_PATH, {'amount': 'abc'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {'error': 'amount must be a decimal number'},
        )

    # -- response value shapes ---------------------------------------------
    def test_total_is_decimal_string_and_date_is_iso_string(self):
        body = self.search(invoice_number='INV-0263')
        row = body['results'][0]
        self.assertIsInstance(row['total'], str,
                              'total must be a decimal STRING, not a JSON number')
        self.assertEqual(Decimal(row['total']), Decimal('256676.23'))
        self.assertIsInstance(row['date'], str)
        self.assertEqual(row['date'], '2025-09-26')

    # -- tenant filtering ----------------------------------------------------
    def test_tenant_id_filters_results_to_that_tenant(self):
        body = self.search(tenant_id=self.tenant_b.tenant_id)
        self.assertEqual(self.result_ids(body), {self.doc_b.id})

        body = self.search(tenant_id=self.tenant.tenant_id, q='tenantb-doc')
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['results'], [])

    # -- invented adversarial cases ------------------------------------------
    def test_q_sql_wildcard_percent_is_literal(self):
        # Unescaped LIKE would make '%' match every document.
        body = self.search(q='%')
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['results'], [])

    def test_q_sql_wildcard_underscore_is_literal(self):
        # Unescaped LIKE would let 'WBRX_' match 'WBRXZreport.pdf' too.
        body = self.search(q='WBRX_')
        self.assertEqual(self.result_ids(body), {self.doc_underscore.id})

    def test_q_with_no_match_returns_empty_not_everything(self):
        body = self.search(q='zzz-no-such-token-anywhere')
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['results'], [])

    def test_invoice_number_and_amount_combine_with_AND(self):
        # invoice_number belongs to invoice1, amount to invoice2 — an OR
        # implementation would return one or both; the contract is AND.
        body = self.search(invoice_number='INV-0263', amount='1500.00')
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['results'], [])

    def test_url_names_resolve_to_contracted_paths(self):
        from django.urls import reverse
        self.assertEqual(reverse('xero_data:document_search'), SEARCH_PATH)
        self.assertEqual(
            reverse('xero_data:document_file', kwargs={'document_id': 42}),
            FILE_PATH_TEMPLATE.format(document_id=42),
        )


class DocumentFileSignedUrlTests(DocumentSearchFixtureMixin, TestCase):
    """Contract tests for GET /xero/data/documents/<id>/file/?s=<sig>."""

    @staticmethod
    def response_bytes(response):
        if getattr(response, 'streaming', False):
            return b''.join(response.streaming_content)
        return response.content

    # (10) -------------------------------------------------------------------
    def test_valid_signature_serves_exact_bytes_inline(self):
        # Endpoint B is PUBLIC: plain unauthenticated client, no JWT header.
        response = self.client.get(
            file_url(self.doc1.id, expected_signature(self.doc1.id))
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.response_bytes(response), DOC1_BYTES)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(
            response['Content-Disposition'].startswith('inline'),
            f'Content-Disposition was {response["Content-Disposition"]!r}',
        )

    # (11) -------------------------------------------------------------------
    def test_tampered_signature_is_403(self):
        sig = expected_signature(self.doc1.id)
        tampered = ('0' if sig[0] != '0' else '1') + sig[1:]
        response = self.client.get(file_url(self.doc1.id, tampered))
        self.assertEqual(response.status_code, 403)

    def test_missing_signature_is_403(self):
        response = self.client.get(file_url(self.doc1.id))
        self.assertEqual(response.status_code, 403)

    def test_signature_for_a_different_id_is_403(self):
        # A sig that is perfectly valid — for another document. Accepting it
        # would allow enumeration of every stored document.
        other_sig = expected_signature(self.doc2.id)
        response = self.client.get(file_url(self.doc1.id, other_sig))
        self.assertEqual(response.status_code, 403)

    # (12) -------------------------------------------------------------------
    def test_valid_signature_for_unknown_id_is_404(self):
        from apps.xero.xero_data.models import XeroDocument
        missing_id = (XeroDocument.objects.order_by('-id')
                      .values_list('id', flat=True).first() or 0) + 987654
        response = self.client.get(
            file_url(missing_id, expected_signature(missing_id))
        )
        self.assertEqual(response.status_code, 404)

    # (13) -------------------------------------------------------------------
    def test_view_url_from_search_actually_serves_the_file(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        body = client.get(SEARCH_PATH, {'invoice_number': 'INV-0263'}).json()
        self.assertEqual(len(body['results']), 1)
        view_url = body['results'][0]['view_url']

        parts = urlsplit(view_url)
        self.assertIn(parts.scheme, ('http', 'https'),
                      f'view_url must be absolute, got {view_url!r}')
        self.assertTrue(parts.netloc, f'view_url must be absolute, got {view_url!r}')

        # Fetch it exactly as a browser would — unauthenticated. The signed URL
        # carries the PUBLIC base (SLIP_VIEW_BASE_URL, '…/backend'), but nginx
        # strips that prefix before Django ever sees the request, so the test
        # client — which talks to Django directly — must strip it too.
        base_prefix = urlsplit(
            getattr(settings, 'SLIP_VIEW_BASE_URL', 'https://console.8-bit.space/backend')
        ).path.rstrip('/')
        path = parts.path
        if base_prefix and path.startswith(base_prefix):
            path = path[len(base_prefix):]
        response = self.client.get(f'{path}?{parts.query}')
        self.assertEqual(
            response.status_code, 200,
            f'view_url {view_url!r} did not serve the file '
            f'(status {response.status_code}) — base or signature mismatch',
        )
        self.assertEqual(self.response_bytes(response), DOC1_BYTES)

    # -- helper cross-check ----------------------------------------------------
    def test_signature_helper_matches_contract_hmac(self):
        from apps.xero.xero_data.document_views import xero_document_signature
        self.assertEqual(
            xero_document_signature(self.doc1.id),
            expected_signature(self.doc1.id),
        )

    def test_url_helper_signs_correctly(self):
        from apps.xero.xero_data.document_views import xero_document_url
        url = xero_document_url(self.doc1.id, base='http://testserver')
        parts = urlsplit(url)
        response = self.client.get(f'{parts.path}?{parts.query}')
        self.assertEqual(response.status_code, 200)


class DocumentSearchNoXeroCredentialsTest(DocumentSearchFixtureMixin, TestCase):
    """(14) The search must be a pure-DB operation.

    The fixture creates NO XeroClientCredentials and NO XeroTenantToken, so
    any code path that phones Xero (token refresh, attachment probe) will
    blow up or hang here. A plain search must still return 200 from the DB.
    """

    def test_search_works_with_absent_xero_credential_set(self):
        from apps.xero.xero_auth.models import XeroClientCredentials, XeroTenantToken
        self.assertEqual(XeroClientCredentials.objects.count(), 0)
        self.assertEqual(XeroTenantToken.objects.count(), 0)

        body = self.search(q='engen-slip')
        self.assertEqual(body['count'], 1)
