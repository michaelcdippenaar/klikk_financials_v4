"""
Adversarial tests for apps.receipts (Audit -> Receipts review over whatsapp.klikk_slips).

``whatsapp.klikk_slips`` is NOT a Django model (external WhatsApp sync), so the
test database does not contain it. Every test class below creates it with raw
DDL inside the class-level transaction (Postgres DDL is transactional, so the
table is rolled back with the rest of the class fixture) and populates it with
production-shaped rows: NULL slip_ts, NULL ocr, non-numeric / negative / missing
``ocr->>'total'``, ``MATCHED (auto-recon ...)`` status variants, multi-line
journals, journal numbers that collide across Xero tenants, and a real byte blob
that must never surface in any JSON/CSV/XLSX response.

Run:  manage.py test apps.receipts
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import unittest
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.audit.slip_view import slip_signature
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.models import XeroJournals
from apps.xero.xero_metadata.models import XeroAccount, XeroContacts

from .models import SlipComment, SlipReview
from .services import SLIP_TZ, fy_label, fy_range

UTC = dt.timezone.utc
User = get_user_model()

KLIKK_SLIPS_DDL = """
CREATE SCHEMA IF NOT EXISTS whatsapp;
CREATE TABLE IF NOT EXISTS whatsapp.klikk_slips (
  sha256 text primary key,
  slip_ts timestamptz,
  filename text,
  source text,
  mime_ext text,
  byte_size bigint,
  file_bytes bytea,
  xero_status text not null default 'PENDING',
  xero_detail text,
  imported_at timestamptz not null default now(),
  synced_to_xero boolean not null default false,
  ocr jsonb,
  search_tsv tsvector,
  journal_number integer,
  xero_org text
);
"""

# A recognisable blob. If any byte of this shows up in a list/detail/export body, the
# endpoint is leaking ``file_bytes``.
BLOB = b'\xff\xd8\xff\xe0LEAKED_FILE_BYTES_SENTINEL_0123456789' * 20
BLOB_MARKERS = ('LEAKED_FILE_BYTES_SENTINEL', 'file_bytes', '/9j/4', '\\xff\\xd8')


def sha(n: int) -> str:
    """Deterministic 64-hex sha256-shaped key."""
    return f'{n:064x}'


def ts(y, m, d, hh=0, mm=0, ss=0, tz=UTC) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=tz)


def create_slips_table():
    with connection.cursor() as cur:
        cur.execute(KLIKK_SLIPS_DDL)


def insert_slip(sha256, *, slip_ts=None, filename='PHOTO.jpg', source='chat_export', mime_ext='jpg',
                byte_size=None, file_bytes=None, xero_status='PENDING', xero_detail=None, synced=False,
                ocr=None, journal_number=None, xero_org=None, tsv_text=None, ocr_raw=None):
    """
    Insert one production-shaped row. ``ocr`` is a dict (json-dumped); ``ocr_raw`` lets a
    test hand in a raw jsonb literal (e.g. a JSON string / array instead of an object).
    ``search_tsv`` is populated from ``tsv_text`` (or supplier+filename) to mirror the
    sync's trigger, since the ``q`` filter reads it.
    """
    if ocr_raw is None:
        ocr_raw = json.dumps(ocr) if ocr is not None else None
    if tsv_text is None:
        sup = (ocr or {}).get('supplier') if isinstance(ocr, dict) else None
        tsv_text = ' '.join(x for x in (sup, filename) if x)
    with connection.cursor() as cur:
        cur.execute(
            """
            insert into whatsapp.klikk_slips
              (sha256, slip_ts, filename, source, mime_ext, byte_size, file_bytes, xero_status, xero_detail,
               synced_to_xero, ocr, search_tsv, journal_number, xero_org)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, to_tsvector('simple', %s), %s, %s)
            """,
            [sha256, slip_ts, filename, source, mime_ext,
             byte_size if byte_size is not None else (len(file_bytes) if file_bytes else None),
             file_bytes, xero_status, xero_detail, synced, ocr_raw, tsv_text or '', journal_number, xero_org],
        )


def make_tenant(tenant_id, name):
    return XeroTenant.objects.create(tenant_id=tenant_id, tenant_name=name)


def make_account(tenant, account_id, code, name, type_):
    # bulk_create: skips post_save. apps/xero/xero_metadata/signals.py writes
    # GlossaryRefreshRequest.organisation_id (IntegerField) from a UUID-string tenant pk and
    # raises ValueError on every ORM save() of an account/contact — pre-existing bug, not receipts'.
    return XeroAccount.objects.bulk_create(
        [XeroAccount(organisation=tenant, account_id=account_id, code=code, name=name, type=type_)])[0]


def make_contact(tenant, contacts_id, name):
    return XeroContacts.objects.bulk_create([XeroContacts(organisation=tenant, contacts_id=contacts_id, name=name)])[0]


def make_line(tenant, journal_number, account, amount, *, journal_id, date, description='', contact=None):
    amount = Decimal(amount)
    return XeroJournals.objects.create(
        organisation=tenant, journal_id=journal_id, journal_number=journal_number, journal_type='journal',
        account=account, contact=contact, date=date, description=description,
        amount=amount, debit=amount if amount > 0 else Decimal('0'), credit=amount if amount < 0 else Decimal('0'),
        tax_amount=Decimal('0'),
    )


def body_has_blob(text: str) -> bool:
    return any(m in text for m in BLOB_MARKERS)


# --------------------------------------------------------------------------- #
# Shared fixture
# --------------------------------------------------------------------------- #
class ReceiptsFixtureMixin:
    """
    12 slips covering every production shape. Keyed by name for readability.

    name          sha      slip_ts (UTC)            status                         total      cat        journal / org     notes
    ----------    ------   ----------------------   ----------------------------   --------   --------   ---------------   -----
    a_fy26        1        2025-08-10 10:00         MATCHED                        259.00     Hardware   697 / Klikk       5-line journal, #697 collides with tenant B
    b_fy26_auto   2        2025-12-09 19:58         MATCHED (auto-recon 2026-08-17) 1234.56   Meals      962018 / Klikk    2-line journal, synced
    c_fy25_pend   3        2025-03-01 08:00         PENDING                        40.00      Fuel       -                 blob row (file_bytes set)
    d_fy26_nix    4        2026-02-14 12:00         NOT IN XERO                    99.99      Hardware   -
    e_skip_neg    5        2026-03-18 07:25         skipped (no/negative total)    -259.00    Hardware   -                 negative total => NULL numeric
    f_ocr_abc     6        2026-04-01 09:00         PENDING                        "abc"      Meals      -                 non-numeric
    g_ocr_empty   7        2026-04-02 09:00         PENDING                        ""         -          -                 empty string
    h_ocr_nototal 8        2026-04-03 09:00         PENDING                        (missing)  Meals      -                 no 'total' key
    i_ocr_null    9        2026-04-04 09:00         PENDING                        NULL ocr   -          -                 ocr IS NULL
    j_ts_null     10       NULL                     PENDING                        15.00      Meals      -                 slip_ts IS NULL (exists in prod)
    k_fy27        11       2026-07-15 06:00         PENDING                        500.00     Fuel       -                 FY27
    l_orphan_jn   12       2025-09-09 09:00         MATCHED                        77.00      Meals      555 / Klikk       journal number with NO lines anywhere
    """

    NAMES = {
        'a_fy26': sha(1), 'b_fy26_auto': sha(2), 'c_fy25_pend': sha(3), 'd_fy26_nix': sha(4),
        'e_skip_neg': sha(5), 'f_ocr_abc': sha(6), 'g_ocr_empty': sha(7), 'h_ocr_nototal': sha(8),
        'i_ocr_null': sha(9), 'j_ts_null': sha(10), 'k_fy27': sha(11), 'l_orphan_jn': sha(12),
    }
    NUMERIC_TOTALS = {'a_fy26': '259.00', 'b_fy26_auto': '1234.56', 'c_fy25_pend': '40.00', 'd_fy26_nix': '99.99',
                      'j_ts_null': '15.00', 'k_fy27': '500.00', 'l_orphan_jn': '77.00'}
    FIXTURE_SUM = sum(Decimal(v) for v in NUMERIC_TOTALS.values())  # 2225.55

    @classmethod
    def setUpTestData(cls):
        create_slips_table()
        S = cls.NAMES

        # ---- Xero fixtures: two tenants, journal #697 in BOTH (the cross-tenant trap) ----
        cls.klikk = make_tenant('tenant-klikk', 'Klikk (Pty) Ltd')
        cls.dfam = make_tenant('tenant-dfam', 'Dippenaar Family')

        k_bank = make_account(cls.klikk, 'acc-k-bank', '090', 'Investec Current', 'BANK')
        k_hw = make_account(cls.klikk, 'acc-k-hw', '429', 'Hardware & Consumables', 'EXPENSE')
        k_vat = make_account(cls.klikk, 'acc-k-vat', '820', 'VAT', 'CURRLIAB')
        k_meals = make_account(cls.klikk, 'acc-k-meals', '420', 'Entertainment', 'EXPENSE')
        d_bank = make_account(cls.dfam, 'acc-d-bank', '090', 'FNB Cheque', 'BANK')
        d_groc = make_account(cls.dfam, 'acc-d-groc', '450', 'Groceries', 'EXPENSE')

        builders = make_contact(cls.klikk, 'con-k-builders', 'Builders Warehouse')
        woolies = make_contact(cls.dfam, 'con-d-woolies', 'Woolworths')
        jonnys = make_contact(cls.klikk, 'con-k-jonnys', 'Jonnys Mozambican Restaurant')

        d697 = ts(2025, 8, 10, 0, 0)
        # Klikk #697: 5 lines (hardware 200 + hardware 25.22 + VAT 33.78 + bank -259 + a zero line)
        make_line(cls.klikk, 697, k_hw, '200.00', journal_id='k697-1', date=d697, description='Drill bits', contact=builders)
        make_line(cls.klikk, 697, k_hw, '25.22', journal_id='k697-2', date=d697, description='Screws', contact=builders)
        make_line(cls.klikk, 697, k_vat, '33.78', journal_id='k697-3', date=d697, description='VAT on Builders', contact=builders)
        make_line(cls.klikk, 697, k_bank, '-259.00', journal_id='k697-4', date=d697, description='Builders Warehouse card', contact=builders)
        make_line(cls.klikk, 697, k_hw, '0.00', journal_id='k697-5', date=d697, description='rounding')
        # Dippenaar Family #697: totally different transaction (the collision)
        d697b = ts(2024, 2, 2, 0, 0)
        make_line(cls.dfam, 697, d_groc, '812.40', journal_id='d697-1', date=d697b, description='Woolies groceries', contact=woolies)
        make_line(cls.dfam, 697, d_bank, '-812.40', journal_id='d697-2', date=d697b, description='Woolies groceries', contact=woolies)
        # Klikk #962018: 2 lines
        d962 = ts(2025, 12, 9, 0, 0)
        make_line(cls.klikk, 962018, k_meals, '1234.56', journal_id='k962018-1', date=d962, description='Team dinner', contact=jonnys)
        make_line(cls.klikk, 962018, k_bank, '-1234.56', journal_id='k962018-2', date=d962, description='Team dinner', contact=jonnys)

        # ---- Slips ----
        insert_slip(S['a_fy26'], slip_ts=ts(2025, 8, 10, 10, 0), filename='00000292-PHOTO-2025-08-10-12-00-00.jpg',
                    xero_status='MATCHED', xero_detail='journal_number=697', synced=True,
                    ocr={'supplier': 'Builders Warehouse', 'total': '259.00', 'category': 'Hardware',
                         'slip_date': '2025-08-10', 'payment_method': 'Card',
                         'items': [{'description': 'Drill bits', 'amount': '200.00'}, {'description': 'Screws', 'amount': '25.22'}]},
                    journal_number=697, xero_org='Klikk (Pty) Ltd')
        insert_slip(S['b_fy26_auto'], slip_ts=ts(2025, 12, 9, 19, 58, 48), filename='00000612-PHOTO-2025-12-09-21-58-48.jpg',
                    xero_status='MATCHED (auto-recon 2026-08-17)', xero_detail='Slip date=2025-12-09; Supplier=Jonnys Mozambican Restaurant;',
                    synced=True, ocr={'supplier': 'Jonnys Mozambican Restaurant', 'total': '1234.56', 'category': 'Meals',
                                      'slip_date': '2025-12-09', 'payment_method': 'Card'},
                    journal_number=962018, xero_org='Klikk (Pty) Ltd')
        insert_slip(S['c_fy25_pend'], slip_ts=ts(2025, 3, 1, 8, 0), filename='PHOTO-2025-03-01-10-00-00.jpg', source='archive5_2026',
                    xero_status='PENDING', file_bytes=BLOB,
                    ocr={'supplier': 'Engen Stellenbosch', 'total': '40.00', 'category': 'Fuel', 'slip_date': '2025-03-01'})
        insert_slip(S['d_fy26_nix'], slip_ts=ts(2026, 2, 14, 12, 0), filename='PHOTO-2026-02-14-14-00-00.jpg',
                    xero_status='NOT IN XERO', ocr={'supplier': 'Takealot', 'total': '99.99', 'category': 'Hardware'})
        insert_slip(S['e_skip_neg'], slip_ts=ts(2026, 3, 18, 7, 25, 45), filename='PHOTO-2026-03-18-09-25-45 3.jpg',
                    xero_status='skipped (no/negative total)', xero_detail='refund',
                    ocr={'supplier': 'Builders Warehouse', 'total': '-259.00', 'category': 'Hardware',
                         'payment_method': 'Card (Tap) - Refund'})
        insert_slip(S['f_ocr_abc'], slip_ts=ts(2026, 4, 1, 9, 0), filename='PHOTO-2026-04-01.jpg',
                    ocr={'supplier': 'Spur', 'total': 'abc', 'category': 'Meals'})
        insert_slip(S['g_ocr_empty'], slip_ts=ts(2026, 4, 2, 9, 0), filename='PHOTO-2026-04-02.jpg',
                    ocr={'supplier': 'Unknown', 'total': ''})
        insert_slip(S['h_ocr_nototal'], slip_ts=ts(2026, 4, 3, 9, 0), filename='PHOTO-2026-04-03.jpg',
                    ocr={'supplier': 'Nandos', 'category': 'Meals'})
        insert_slip(S['i_ocr_null'], slip_ts=ts(2026, 4, 4, 9, 0), filename='PHOTO-2026-04-04.pdf', mime_ext='pdf', ocr=None)
        insert_slip(S['j_ts_null'], slip_ts=None, filename='no-timestamp.jpg',
                    ocr={'supplier': 'KFC', 'total': '15.00', 'category': 'Meals'})
        insert_slip(S['k_fy27'], slip_ts=ts(2026, 7, 15, 6, 0), filename='PHOTO-2026-07-15.jpg',
                    ocr={'supplier': 'Shell', 'total': '500.00', 'category': 'Fuel'})
        insert_slip(S['l_orphan_jn'], slip_ts=ts(2025, 9, 9, 9, 0), filename='PHOTO-2025-09-09.jpg',
                    xero_status='MATCHED', synced=True, journal_number=555, xero_org='Klikk (Pty) Ltd',
                    ocr={'supplier': 'Wimpy', 'total': '77.00', 'category': 'Meals'})

        cls.user = User.objects.create_user(username='reviewer', email='reviewer@example.com', password='pw-irrelevant')

    # ---- helpers ----
    def setUp(self):
        self.client = APIClient()

    def auth(self):
        token = str(RefreshToken.for_user(self.user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def list_(self, **params):
        resp = self.client.get(reverse('receipts:list'), params)
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        return resp.json()

    def shas(self, data) -> list[str]:
        return [r['sha256'] for r in data['results']]

    def names(self, data) -> set[str]:
        inv = {v: k for k, v in self.NAMES.items()}
        return {inv.get(s, s) for s in self.shas(data)}

    def export(self, **params):
        return self.client.get(reverse('receipts:export'), params)

    def csv_rows(self, resp):
        return list(csv.reader(io.StringIO(resp.content.decode('utf-8'))))


# --------------------------------------------------------------------------- #
# 1. List: shape, filters, pagination, totals
# --------------------------------------------------------------------------- #
class ListShapeTests(ReceiptsFixtureMixin, TestCase):
    def test_unfiltered_list_returns_every_slip_once(self):
        data = self.list_(page_size=200)
        self.assertEqual(data['count'], 12)
        self.assertEqual(len(data['results']), 12)
        self.assertEqual(len(set(self.shas(data))), 12, 'duplicate rows in list')
        self.assertEqual(data['num_pages'], 1)
        self.assertEqual(data['page'], 1)

    def test_row_shape_and_no_file_bytes(self):
        data = self.list_(page_size=200)
        row = next(r for r in data['results'] if r['sha256'] == self.NAMES['a_fy26'])
        for key in ('sha256', 'slip_ts', 'filename', 'source', 'mime_ext', 'mime', 'is_pdf', 'byte_size', 'xero_status',
                    'status_group', 'xero_detail', 'synced_to_xero', 'journal_number', 'xero_org', 'fy', 'supplier',
                    'total', 'category', 'slip_date', 'payment_method', 'view_url', 'journal', 'review', 'comment_count'):
            self.assertIn(key, row)
        self.assertNotIn('file_bytes', row)
        self.assertNotIn('ocr', row, 'list must not ship the full ocr blob per row')
        self.assertEqual(row['total'], '259.00')
        self.assertEqual(row['fy'], 'FY26')
        self.assertEqual(row['status_group'], 'MATCHED')
        self.assertEqual(row['mime'], 'image/jpeg')
        self.assertFalse(row['is_pdf'])

    def test_blob_row_does_not_leak_bytes_in_list_or_detail(self):
        resp = self.client.get(reverse('receipts:list'), {'page_size': 200})
        self.assertFalse(body_has_blob(resp.content.decode('utf-8', 'replace')), 'file_bytes leaked into list JSON')
        # The blob row is ~1.2KB; the whole list must be well under raw-blob size * rows.
        detail = self.client.get(reverse('receipts:detail', args=[self.NAMES['c_fy25_pend']]))
        self.assertEqual(detail.status_code, 200)
        text = detail.content.decode('utf-8', 'replace')
        self.assertFalse(body_has_blob(text), 'file_bytes leaked into detail JSON')
        self.assertNotIn('file_bytes', detail.json())
        self.assertEqual(detail.json()['byte_size'], len(BLOB))

    def test_pdf_row_mime(self):
        data = self.list_(page_size=200)
        row = next(r for r in data['results'] if r['sha256'] == self.NAMES['i_ocr_null'])
        self.assertTrue(row['is_pdf'])
        self.assertEqual(row['mime'], 'application/pdf')
        self.assertIsNone(row['total'])
        self.assertIsNone(row['supplier'])

    def test_totals_cover_whole_filter_set_not_current_page(self):
        data = self.list_(page_size=3, page=2)
        self.assertEqual(data['count'], 12)
        self.assertEqual(data['num_pages'], 4)
        self.assertEqual(len(data['results']), 3)
        self.assertEqual(data['totals']['count'], 12)
        self.assertEqual(Decimal(data['totals']['sum_total']), self.FIXTURE_SUM)
        # Page 4 is the short page, totals must be unchanged.
        data4 = self.list_(page_size=3, page=4)
        self.assertEqual(len(data4['results']), 3)
        self.assertEqual(Decimal(data4['totals']['sum_total']), self.FIXTURE_SUM)

    def test_sum_total_ignores_non_numeric_and_negative_totals(self):
        # -259.00, "abc", "", missing key, NULL ocr must all be excluded, not crash.
        data = self.list_(page_size=200)
        self.assertEqual(Decimal(data['totals']['sum_total']), self.FIXTURE_SUM)
        by_sha = {r['sha256']: r for r in data['results']}
        self.assertIsNone(by_sha[self.NAMES['e_skip_neg']]['total'],
                          'negative total is rendered as a number; TOTAL_SQL regex was meant to null it')
        self.assertIsNone(by_sha[self.NAMES['f_ocr_abc']]['total'])
        self.assertIsNone(by_sha[self.NAMES['g_ocr_empty']]['total'])
        self.assertIsNone(by_sha[self.NAMES['h_ocr_nototal']]['total'])
        self.assertIsNone(by_sha[self.NAMES['i_ocr_null']]['total'])

    # --- pagination edges ---
    def test_pagination_edges(self):
        self.assertEqual(self.list_(page=0)['page'], 1)
        self.assertEqual(self.list_(page=-1)['page'], 1)
        self.assertEqual(self.list_(page='abc')['page'], 1)
        far = self.list_(page=99999, page_size=5)
        self.assertEqual(far['count'], 12)
        self.assertEqual(far['results'], [])
        self.assertEqual(far['num_pages'], 3)
        self.assertEqual(self.list_(page_size=0)['page_size'], 1)
        self.assertEqual(self.list_(page_size=-5)['page_size'], 1)
        self.assertEqual(self.list_(page_size=100000)['page_size'], 200)
        self.assertEqual(self.list_(page_size='x')['page_size'], 50)
        empty = self.list_(q='zzz-nothing-matches')
        self.assertEqual(empty['count'], 0)
        self.assertEqual(empty['num_pages'], 1)
        self.assertEqual(empty['results'], [])
        self.assertEqual(empty['totals'], {'count': 0, 'sum_total': '0.00'})

    def test_pages_partition_results_without_overlap_or_loss(self):
        seen = []
        for p in (1, 2, 3, 4, 5):
            seen += self.shas(self.list_(page=p, page_size=3, ordering='slip_ts'))
        self.assertEqual(len(seen), 12)
        self.assertEqual(len(set(seen)), 12)


class ListFilterTests(ReceiptsFixtureMixin, TestCase):
    def test_q_matches_supplier_via_tsv(self):
        self.assertEqual(self.names(self.list_(q='Builders')), {'a_fy26', 'e_skip_neg'})

    def test_q_matches_filename_ilike(self):
        self.assertEqual(self.names(self.list_(q='no-timestamp')), {'j_ts_null'})

    def test_q_is_case_insensitive_and_multiword(self):
        self.assertEqual(self.names(self.list_(q='jonnys mozambican')), {'b_fy26_auto'})
        self.assertEqual(self.names(self.list_(q='JONNYS')), {'b_fy26_auto'})

    def test_q_junk_and_sqli_do_not_500(self):
        for junk in ("'; DROP TABLE whatsapp.klikk_slips; --", "%", "_", "\\", "' or 1=1 --", "a & b | c ! d", "(((", "'"):
            data = self.list_(q=junk)
            self.assertIn('count', data)
        # Table still there (the DROP did not run).
        self.assertEqual(self.list_()['count'], 12)

    def test_synced_filter(self):
        self.assertEqual(self.names(self.list_(synced='true')), {'a_fy26', 'b_fy26_auto', 'l_orphan_jn'})
        self.assertEqual(self.list_(synced='false')['count'], 9)
        self.assertEqual(self.list_(synced='1')['count'], 3)
        self.assertEqual(self.list_(synced='maybe')['count'], 12, 'junk synced value must be ignored')

    def test_status_prefix_matching(self):
        self.assertEqual(self.names(self.list_(status='MATCHED')), {'a_fy26', 'b_fy26_auto', 'l_orphan_jn'},
                         'status=MATCHED must include "MATCHED (auto-recon ...)" rows')
        self.assertEqual(self.names(self.list_(status='matched')), {'a_fy26', 'b_fy26_auto', 'l_orphan_jn'})
        self.assertEqual(self.names(self.list_(status='SKIPPED')), {'e_skip_neg'})
        self.assertEqual(self.names(self.list_(status='skipped')), {'e_skip_neg'})
        self.assertEqual(self.names(self.list_(status='NOT IN XERO')), {'d_fy26_nix'})
        self.assertEqual(self.list_(status='PENDING')['count'], 7)
        self.assertEqual(self.list_(status='BANANA')['count'], 0)
        self.assertEqual(self.list_(status="'; drop table x; --")['count'], 0)

    def test_status_group_labels(self):
        groups = {r['sha256']: r['status_group'] for r in self.list_(page_size=200)['results']}
        self.assertEqual(groups[self.NAMES['b_fy26_auto']], 'MATCHED')
        self.assertEqual(groups[self.NAMES['e_skip_neg']], 'SKIPPED')
        self.assertEqual(groups[self.NAMES['d_fy26_nix']], 'NOT IN XERO')
        self.assertEqual(groups[self.NAMES['c_fy25_pend']], 'PENDING')

    def test_category_filter_is_exact_case_insensitive(self):
        self.assertEqual(self.names(self.list_(category='hardware')), {'a_fy26', 'd_fy26_nix', 'e_skip_neg'})
        self.assertEqual(self.names(self.list_(category='Fuel')), {'c_fy25_pend', 'k_fy27'})
        self.assertEqual(self.list_(category='Hard')['count'], 0, 'category is an exact match, not a prefix')
        self.assertEqual(self.list_(category="x'; drop table x; --")['count'], 0)

    def test_min_max_total(self):
        self.assertEqual(self.names(self.list_(min_total='100')), {'a_fy26', 'b_fy26_auto', 'k_fy27'})
        self.assertEqual(self.names(self.list_(min_total='259.01')), {'b_fy26_auto', 'k_fy27'})
        self.assertEqual(self.names(self.list_(min_total='259.00')), {'a_fy26', 'b_fy26_auto', 'k_fy27'}, 'bounds are inclusive')
        self.assertEqual(self.names(self.list_(max_total='40')), {'c_fy25_pend', 'j_ts_null'})
        self.assertEqual(self.names(self.list_(min_total='50', max_total='300')), {'a_fy26', 'd_fy26_nix', 'l_orphan_jn'})
        # Rows whose total is non-numeric / negative / missing never match a numeric bound.
        self.assertEqual(self.list_(min_total='-1000')['count'], 7)
        self.assertEqual(self.list_(max_total='0')['count'], 0)

    def test_min_total_junk_is_ignored_or_harmless(self):
        self.assertEqual(self.list_(min_total='abc')['count'], 12)
        self.assertEqual(self.list_(min_total='')['count'], 12)
        self.assertEqual(self.list_(min_total="1; drop table x")['count'], 12)
        # Decimal() accepts these; Postgres must not choke on them.
        for weird in ('NaN', 'sNaN', 'Infinity', '-Infinity', '1e400', '1e-400', '0x10'):
            data = self.list_(min_total=weird)
            self.assertIn('count', data)
            data = self.list_(max_total=weird)
            self.assertIn('count', data)

    def test_date_range_filters(self):
        self.assertEqual(self.names(self.list_(date_from='2026-04-01', date_to='2026-04-04')),
                         {'f_ocr_abc', 'g_ocr_empty', 'h_ocr_nototal', 'i_ocr_null'})
        self.assertEqual(self.names(self.list_(date_from='2026-07-01')), {'k_fy27'})
        self.assertEqual(self.names(self.list_(date_to='2025-03-01')), {'c_fy25_pend'})
        # NULL slip_ts is excluded by any date bound
        self.assertNotIn('j_ts_null', self.names(self.list_(date_from='1900-01-01')))
        # junk dates ignored
        self.assertEqual(self.list_(date_from='not-a-date')['count'], 12)
        self.assertEqual(self.list_(date_from='2026-13-45')['count'], 12)

    def test_ordering_whitelist_and_sqli(self):
        default = self.shas(self.list_(page_size=200))
        for bad in ('slip_ts; DROP TABLE x', '__proto__', 'file_bytes', '-file_bytes', 's.sha256', 'total desc', '', ' '):
            self.assertEqual(self.shas(self.list_(ordering=bad, page_size=200)), default,
                             f'ordering={bad!r} must fall back to the default ordering')
        self.assertEqual(self.list_()['count'], 12)

    def test_ordering_slip_ts_nulls_last_both_directions(self):
        asc = self.shas(self.list_(ordering='slip_ts', page_size=200))
        desc = self.shas(self.list_(ordering='-slip_ts', page_size=200))
        self.assertEqual(asc[-1], self.NAMES['j_ts_null'])
        self.assertEqual(desc[-1], self.NAMES['j_ts_null'])
        self.assertEqual(asc[0], self.NAMES['c_fy25_pend'])
        self.assertEqual(desc[0], self.NAMES['k_fy27'])
        self.assertEqual(asc[:-1], list(reversed(desc[:-1])))

    def test_ordering_total_with_non_numeric_values(self):
        rows = self.list_(ordering='-total', page_size=200)['results']
        totals = [r['total'] for r in rows]
        numeric = [Decimal(t) for t in totals if t is not None]
        self.assertEqual(numeric, sorted(numeric, reverse=True))
        self.assertEqual(totals[:7], ['1234.56', '500.00', '259.00', '99.99', '77.00', '40.00', '15.00'])
        self.assertTrue(all(t is None for t in totals[7:]), 'non-numeric totals must sort last')
        rows = self.list_(ordering='total', page_size=200)['results']
        self.assertEqual([r['total'] for r in rows][:7], ['15.00', '40.00', '77.00', '99.99', '259.00', '500.00', '1234.56'])

    def test_ordering_supplier_and_status(self):
        rows = self.list_(ordering='supplier', page_size=200)['results']
        sups = [r['supplier'] for r in rows if r['supplier']]
        self.assertEqual(sups, sorted(sups, key=str.lower))
        self.assertIsNone(rows[-1]['supplier'], 'NULL supplier should sort last')
        rows = self.list_(ordering='xero_status', page_size=200)['results']
        sts = [r['xero_status'] for r in rows]
        self.assertEqual(sts, sorted(sts))

    def test_combined_filters_and_totals_agree(self):
        data = self.list_(fy='FY26', status='MATCHED', synced='true', category='Hardware', min_total='100', ordering='-total')
        self.assertEqual(self.names(data), {'a_fy26'})
        self.assertEqual(data['totals'], {'count': 1, 'sum_total': '259.00'})
        data = self.list_(fy='FY26', status='PENDING')
        self.assertEqual(self.names(data), {'f_ocr_abc', 'g_ocr_empty', 'h_ocr_nototal', 'i_ocr_null'})
        self.assertEqual(data['totals'], {'count': 4, 'sum_total': '0.00'})

    def test_to_process_and_decision_filters(self):
        SlipReview.objects.create(sha256=self.NAMES['a_fy26'], to_process=True, decision='CAPTURE')
        SlipReview.objects.create(sha256=self.NAMES['d_fy26_nix'], to_process=True, decision='')
        SlipReview.objects.create(sha256=self.NAMES['f_ocr_abc'], to_process=False, decision='PERSONAL')
        self.assertEqual(self.names(self.list_(to_process='true')), {'a_fy26', 'd_fy26_nix'})
        self.assertEqual(self.list_(to_process='false')['count'], 10)
        self.assertEqual(self.list_(to_process='junk')['count'], 12)
        self.assertEqual(self.names(self.list_(decision='CAPTURE')), {'a_fy26'})
        self.assertEqual(self.names(self.list_(decision='personal')), {'f_ocr_abc'})
        undecided = self.names(self.list_(decision='UNDECIDED'))
        self.assertEqual(len(undecided), 10)
        self.assertIn('d_fy26_nix', undecided, 'a review row with decision="" is still undecided')
        self.assertNotIn('a_fy26', undecided)
        self.assertEqual(self.names(self.list_(decision='none')), undecided)
        # combination
        self.assertEqual(self.names(self.list_(to_process='true', decision='UNDECIDED')), {'d_fy26_nix'})
        self.assertEqual(self.names(self.list_(to_process='true', decision='CAPTURE', fy='FY26')), {'a_fy26'})
        # totals follow the filter
        self.assertEqual(self.list_(to_process='true')['totals'], {'count': 2, 'sum_total': '358.99'})

    def test_decision_junk_does_not_500(self):
        for junk in ('BANANA', "'; drop table x; --", '__proto__', ' '):
            self.assertIn('count', self.list_(decision=junk))

    def test_to_process_false_with_no_reviews_at_all(self):
        # empty array param -> `not (sha = any('{}'))` must still return everything.
        self.assertEqual(self.list_(to_process='false')['count'], 12)
        self.assertEqual(self.list_(to_process='true')['count'], 0)
        self.assertEqual(self.list_(decision='UNDECIDED')['count'], 12)
        self.assertEqual(self.list_(decision='CAPTURE')['count'], 0)

    def test_review_state_and_comment_count_attached(self):
        SlipReview.objects.create(sha256=self.NAMES['a_fy26'], to_process=True, decision='CAPTURE', note='n', updated_by='mc')
        SlipComment.objects.create(sha256=self.NAMES['a_fy26'], text='one', author='mc')
        SlipComment.objects.create(sha256=self.NAMES['a_fy26'], text='two', author='mc')
        rows = {r['sha256']: r for r in self.list_(page_size=200)['results']}
        a = rows[self.NAMES['a_fy26']]
        self.assertEqual(a['comment_count'], 2)
        self.assertEqual(a['review']['decision'], 'CAPTURE')
        self.assertTrue(a['review']['to_process'])
        self.assertEqual(a['review']['updated_by'], 'mc')
        b = rows[self.NAMES['b_fy26_auto']]
        self.assertEqual(b['comment_count'], 0)
        self.assertEqual(b['review'], {'to_process': False, 'decision': '', 'note': '', 'updated_by': '', 'updated_at': None})


# --------------------------------------------------------------------------- #
# 2. FY bucketing
# --------------------------------------------------------------------------- #
class FiscalYearTests(TestCase):
    """Pure helpers + SQL boundary behaviour. FY = Jul..Jun named by the ENDING year, read in SLIP_TZ (SAST)."""

    @classmethod
    def setUpTestData(cls):
        create_slips_table()
        SAST = SLIP_TZ
        cls.rows = {
            # --- SAST-expressed boundaries ---
            'fy25_last_minute_sast': (sha(101), ts(2025, 6, 30, 23, 59, tz=SAST), 'FY25'),
            'fy26_first_minute_sast': (sha(102), ts(2025, 7, 1, 0, 0, tz=SAST), 'FY26'),
            'fy26_last_minute_sast': (sha(103), ts(2026, 6, 30, 23, 59, tz=SAST), 'FY26'),
            'fy27_first_minute_sast': (sha(104), ts(2026, 7, 1, 0, 0, tz=SAST), 'FY27'),
            # --- The UTC/SAST straddle: 2025-06-30T22:30Z == 2025-07-01 00:30 SAST -> FY26 under SLIP_TZ ---
            'straddle_utc_jun30_is_sast_jul1': (sha(105), ts(2025, 6, 30, 22, 30), 'FY26'),
            # --- The reverse straddle: 2026-06-30T21:59:59Z == 2026-06-30 23:59:59 SAST -> still FY26 ---
            'straddle_utc_jun30_2159_is_sast_jun30': (sha(106), ts(2026, 6, 30, 21, 59, 59), 'FY26'),
            # --- 2026-06-30T22:00:00Z == 2026-07-01 00:00 SAST -> FY27 ---
            'straddle_utc_jun30_2200_is_sast_jul1': (sha(107), ts(2026, 6, 30, 22, 0, 0), 'FY27'),
            # --- NULL timestamp (exists in production) ---
            'null_ts': (sha(108), None, None),
        }
        for name, (s, when, _fy) in cls.rows.items():
            insert_slip(s, slip_ts=when, filename=f'{name}.jpg', ocr={'supplier': name, 'total': '1.00'})

    def setUp(self):
        self.client = APIClient()

    def list_(self, **params):
        resp = self.client.get(reverse('receipts:list'), params)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return resp.json()

    # --- pure helpers ---
    def test_fy_range(self):
        self.assertEqual(fy_range('FY26'), (dt.date(2025, 7, 1), dt.date(2026, 6, 30)))
        self.assertEqual(fy_range('fy26'), (dt.date(2025, 7, 1), dt.date(2026, 6, 30)))
        self.assertEqual(fy_range('FY2026'), (dt.date(2025, 7, 1), dt.date(2026, 6, 30)))
        self.assertEqual(fy_range(' FY25 '), (dt.date(2024, 7, 1), dt.date(2025, 6, 30)))
        for bad in ('2026', 'FY', 'FY2', 'FY12345', 'FY26; drop', '', None):
            with self.assertRaises(ValueError, msg=bad):
                fy_range(bad)

    def test_fy_label_pure(self):
        self.assertEqual(fy_label(dt.date(2025, 6, 30)), 'FY25')
        self.assertEqual(fy_label(dt.date(2025, 7, 1)), 'FY26')
        self.assertEqual(fy_label(dt.date(2026, 6, 30)), 'FY26')
        self.assertEqual(fy_label(dt.date(2026, 7, 1)), 'FY27')
        self.assertIsNone(fy_label(None))
        # aware datetimes are read in SAST
        self.assertEqual(fy_label(ts(2025, 6, 30, 22, 30)), 'FY26', '22:30Z on 30 Jun is 00:30 SAST on 1 Jul')
        self.assertEqual(fy_label(ts(2025, 6, 30, 21, 59)), 'FY25')
        self.assertEqual(fy_label(ts(2026, 6, 30, 22, 0)), 'FY27')

    # --- SQL + shaped row agree on every boundary ---
    def test_each_row_lands_in_the_documented_fy_in_list_and_filter(self):
        data = self.list_(page_size=200)
        self.assertEqual(data['count'], 8)
        by_sha = {r['sha256']: r for r in data['results']}
        for name, (s, _when, expected) in self.rows.items():
            self.assertEqual(by_sha[s]['fy'], expected, f'{name}: row.fy')
        for fy in ('FY25', 'FY26', 'FY27'):
            got = {r['sha256'] for r in self.list_(fy=fy, page_size=200)['results']}
            want = {s for (s, _w, e) in self.rows.values() if e == fy}
            self.assertEqual(got, want, f'fy={fy} filter set')

    def test_fy26_boundaries_explicit(self):
        got = {r['sha256'] for r in self.list_(fy='FY26')['results']}
        self.assertIn(sha(102), got)   # 2025-07-01 00:00 SAST
        self.assertIn(sha(103), got)   # 2026-06-30 23:59 SAST
        self.assertNotIn(sha(101), got)  # 2025-06-30 23:59 SAST
        self.assertNotIn(sha(104), got)  # 2026-07-01 00:00 SAST

    def test_utc_jun30_2230Z_lands_in_FY26_because_it_is_jul1_sast(self):
        got = {r['sha256'] for r in self.list_(fy='FY26')['results']}
        self.assertIn(sha(105), got)
        self.assertNotIn(sha(105), {r['sha256'] for r in self.list_(fy='FY25')['results']})

    def test_utc_jun30_2200Z_lands_in_FY27_because_it_is_jul1_sast(self):
        self.assertIn(sha(107), {r['sha256'] for r in self.list_(fy='FY27')['results']})
        self.assertNotIn(sha(107), {r['sha256'] for r in self.list_(fy='FY26')['results']})
        self.assertIn(sha(106), {r['sha256'] for r in self.list_(fy='FY26')['results']})

    def test_fy_and_date_filters_use_the_same_sast_day(self):
        # date_from/date_to must agree with the fy bucketing (both SAST) or the console's
        # "FY26" pill and a hand-typed 2025-07-01..2026-06-30 range would disagree.
        fy = {r['sha256'] for r in self.list_(fy='FY26', page_size=200)['results']}
        rng = {r['sha256'] for r in self.list_(date_from='2025-07-01', date_to='2026-06-30', page_size=200)['results']}
        self.assertEqual(fy, rng)

    def test_null_slip_ts_does_not_crash_and_is_excluded_from_fy(self):
        data = self.list_(page_size=200)
        null_row = next(r for r in data['results'] if r['sha256'] == sha(108))
        self.assertIsNone(null_row['slip_ts'])
        self.assertIsNone(null_row['fy'])
        for fy in ('FY25', 'FY26', 'FY27'):
            self.assertNotIn(sha(108), {r['sha256'] for r in self.list_(fy=fy)['results']})
        detail = self.client.get(reverse('receipts:detail', args=[sha(108)]))
        self.assertEqual(detail.status_code, 200)
        self.assertIsNone(detail.json()['fy'])

    def test_bad_fy_label_is_ignored_not_500(self):
        for bad in ('2026', 'FY', 'FY2026-27', "FY26'; drop table x; --", 'fy 26'):
            self.assertEqual(self.list_(fy=bad)['count'], 8, f'fy={bad!r}')


# --------------------------------------------------------------------------- #
# 3. Journal join: duplication + cross-tenant collision
# --------------------------------------------------------------------------- #
class JournalJoinTests(ReceiptsFixtureMixin, TestCase):
    def test_five_line_journal_yields_exactly_one_row(self):
        data = self.list_(page_size=200)
        hits = [r for r in data['results'] if r['sha256'] == self.NAMES['a_fy26']]
        self.assertEqual(len(hits), 1, 'journal join multiplied the slip row')
        self.assertEqual(data['count'], 12)
        # and the export agrees
        rows = self.csv_rows(self.export())
        self.assertEqual(sum(1 for r in rows[1:] if r[-2] == self.NAMES['a_fy26']), 1)

    def test_journal_summary_is_the_expense_side_not_the_bank_side(self):
        row = self.client.get(reverse('receipts:detail', args=[self.NAMES['a_fy26']])).json()
        j = row['journal']
        self.assertIsNotNone(j)
        self.assertEqual(j['journal_number'], 697)
        self.assertEqual(j['account_code'], '429')
        self.assertEqual(j['account_name'], 'Hardware & Consumables')
        self.assertEqual(j['description'], 'Drill bits')
        self.assertEqual(j['contact_name'], 'Builders Warehouse')
        self.assertEqual(Decimal(j['debit']), Decimal('259.00'))
        self.assertEqual(Decimal(j['credit']), Decimal('-259.00'))
        self.assertEqual(Decimal(j['amount']), Decimal('259.00'))
        self.assertTrue(j['date'].startswith('2025-08-10'))

    def test_cross_tenant_journal_697_resolves_to_the_slips_org_only(self):
        # Tenant B also has #697 (812.40 Woolworths groceries). Slip is Klikk -> must see Builders 259.
        row = self.client.get(reverse('receipts:detail', args=[self.NAMES['a_fy26']])).json()
        j = row['journal']
        self.assertEqual(j['contact_name'], 'Builders Warehouse')
        self.assertNotEqual(j['contact_name'], 'Woolworths')
        self.assertEqual(Decimal(j['amount']), Decimal('259.00'), 'amount must not include tenant B lines')
        self.assertNotEqual(Decimal(j['amount']), Decimal('1071.40'), 'tenant A + tenant B summed together')
        self.assertEqual(j['description'], 'Drill bits')

    def test_slip_pointing_at_other_tenant_sees_other_tenants_lines(self):
        insert_slip(sha(201), slip_ts=ts(2024, 2, 2, 10, 0), xero_status='MATCHED', synced=True,
                    journal_number=697, xero_org='Dippenaar Family', ocr={'supplier': 'Woolworths', 'total': '812.40'})
        j = self.client.get(reverse('receipts:detail', args=[sha(201)])).json()['journal']
        self.assertEqual(j['contact_name'], 'Woolworths')
        self.assertEqual(j['account_code'], '450')
        self.assertEqual(Decimal(j['amount']), Decimal('812.40'))

    def test_unknown_org_or_null_org_gives_no_journal_not_a_guess(self):
        insert_slip(sha(202), slip_ts=ts(2025, 1, 1), xero_status='MATCHED', journal_number=697, xero_org='Nonexistent Org',
                    ocr={'total': '1.00'})
        insert_slip(sha(203), slip_ts=ts(2025, 1, 1), xero_status='MATCHED', journal_number=697, xero_org=None,
                    ocr={'total': '1.00'})
        for s in (sha(202), sha(203)):
            row = self.client.get(reverse('receipts:detail', args=[s])).json()
            self.assertIsNone(row['journal'], f'{s}: journal must not be resolved from a different tenant')
            self.assertEqual(row['journal_number'], 697)

    def test_orphan_journal_number_and_no_journal_number(self):
        rows = {r['sha256']: r for r in self.list_(page_size=200)['results']}
        self.assertIsNone(rows[self.NAMES['l_orphan_jn']]['journal'])
        self.assertEqual(rows[self.NAMES['l_orphan_jn']]['journal_number'], 555)
        self.assertIsNone(rows[self.NAMES['c_fy25_pend']]['journal'])
        self.assertIsNone(rows[self.NAMES['c_fy25_pend']]['journal_number'])

    @unittest.expectedFailure  # BUG-4 — remove once the join is scoped by journal_type (or the recon carries one)
    def test_same_org_journal_number_shared_by_journal_and_manual_journal_is_not_blended(self):
        # BUG-4: services.JOURNAL_LATERAL_SQL filters `j.journal_number = s.journal_number` + org only.
        # In production xero_data_xerojournals holds journal_type in {journal, manual_journal,
        # system_journal, transaction} and 1,225 same-org (journal_number) pairs span >1 type; the
        # auto-recon matches slips to BOTH regular and manual journals (xero_detail
        # 'journal_number=216150 (loan acc)' is a manual journal). When a slip's number exists as both a
        # `journal` and a `manual_journal` in the same org, the lateral aggregates BOTH sets of lines:
        # amount/debit/credit are the sum of two unrelated transactions and description/account come
        # from whichever has the larger debit. The slip row does not carry a type, so the endpoint
        # cannot disambiguate — needs a decision (prefer 'journal'? carry type in the register?).
        k_loan = make_account(self.klikk, 'acc-k-loan', '880', 'Loan - MC', 'CURRLIAB')
        k_bank = XeroAccount.objects.get(account_id='acc-k-bank')
        k_meals = XeroAccount.objects.get(account_id='acc-k-meals')
        d = ts(2026, 1, 8)
        # regular journal #777: Spur 300
        make_line(self.klikk, 777, k_meals, '300.00', journal_id='k777-j1', date=d, description='Spur lunch')
        make_line(self.klikk, 777, k_bank, '-300.00', journal_id='k777-j2', date=d, description='Spur lunch')
        # manual journal #777 (same org, same number): loan 1500
        for i, (acc, amt, desc) in enumerate((
            (k_loan, '1000.00', 'Loan repayment A'), (k_loan, '500.00', 'Loan repayment B'), (k_bank, '-1500.00', 'Loan repayment'),
        )):
            XeroJournals.objects.create(
                organisation=self.klikk, journal_id=f'k777-m{i}', journal_number=777, journal_type='manual_journal',
                account=acc, date=d, description=desc, amount=Decimal(amt),
                debit=Decimal(amt) if Decimal(amt) > 0 else 0, credit=Decimal(amt) if Decimal(amt) < 0 else 0, tax_amount=0)
        insert_slip(sha(210), slip_ts=d, xero_status='MATCHED (auto-recon 2026-08-17)', xero_detail='journal_number=777',
                    synced=True, journal_number=777, xero_org='Klikk (Pty) Ltd',
                    ocr={'supplier': 'Spur', 'total': '300.00', 'category': 'Meals'})
        j = self.client.get(reverse('receipts:detail', args=[sha(210)])).json()['journal']
        self.assertIsNotNone(j)
        self.assertIn(Decimal(j['amount']), (Decimal('300.00'), Decimal('1500.00')),
                      f"journal amount {j['amount']} is a blend of a journal and a manual_journal with the same number")
        self.assertIn(j['description'], ('Spur lunch', 'Loan repayment A'))

    def test_two_line_journal(self):
        j = next(r for r in self.list_(page_size=200)['results'] if r['sha256'] == self.NAMES['b_fy26_auto'])['journal']
        self.assertEqual(j['journal_number'], 962018)
        self.assertEqual(j['account_code'], '420')
        self.assertEqual(j['contact_name'], 'Jonnys Mozambican Restaurant')
        self.assertEqual(Decimal(j['amount']), Decimal('1234.56'))


# --------------------------------------------------------------------------- #
# 4. Detail
# --------------------------------------------------------------------------- #
class DetailTests(ReceiptsFixtureMixin, TestCase):
    def test_detail_returns_ocr_items_and_comments_in_created_order(self):
        s = self.NAMES['a_fy26']
        c1 = SlipComment.objects.create(sha256=s, text='first', author='mc')
        c2 = SlipComment.objects.create(sha256=s, text='second', author='mc')
        # force c1 to be newer than c2 in created_at terms? No — assert by created_at ascending.
        resp = self.client.get(reverse('receipts:detail', args=[s]))
        self.assertEqual(resp.status_code, 200)
        row = resp.json()
        self.assertEqual(row['ocr']['supplier'], 'Builders Warehouse')
        self.assertEqual(row['items'], [{'description': 'Drill bits', 'amount': '200.00'}, {'description': 'Screws', 'amount': '25.22'}])
        self.assertEqual([c['id'] for c in row['comments']], [c1.id, c2.id])
        self.assertEqual([c['text'] for c in row['comments']], ['first', 'second'])
        self.assertEqual(row['comment_count'], 2)
        self.assertNotIn('file_bytes', row)
        self.assertNotIn('file_bytes', row['ocr'])

    def test_detail_with_null_ocr_and_missing_items(self):
        row = self.client.get(reverse('receipts:detail', args=[self.NAMES['i_ocr_null']])).json()
        self.assertEqual(row['ocr'], {})
        self.assertEqual(row['items'], [])
        row = self.client.get(reverse('receipts:detail', args=[self.NAMES['h_ocr_nototal']])).json()
        self.assertEqual(row['items'], [])
        self.assertIsNone(row['total'])

    def test_detail_with_non_object_ocr_jsonb(self):
        # jsonb can legally hold a string / array / number; the API must not 500 on them.
        insert_slip(sha(301), slip_ts=ts(2025, 5, 5), ocr_raw='"just a string"')
        insert_slip(sha(302), slip_ts=ts(2025, 5, 5), ocr_raw='[1, 2, 3]')
        insert_slip(sha(303), slip_ts=ts(2025, 5, 5), ocr_raw='{"items": "not-a-list", "total": 12}')
        insert_slip(sha(304), slip_ts=ts(2025, 5, 5), ocr_raw='{"items": [1, "x", null, {"description": "ok", "amount": "1.00"}], "total": "12"}')
        for s in (sha(301), sha(302), sha(303), sha(304)):
            resp = self.client.get(reverse('receipts:detail', args=[s]))
            self.assertEqual(resp.status_code, 200, f'{s}: {resp.content[:300]}')
        self.assertEqual(self.client.get(reverse('receipts:detail', args=[sha(304)])).json()['items'],
                         [{'description': 'ok', 'amount': '1.00'}])
        # numeric json total (12, not "12") — ->> renders it as '12' so it is numeric
        self.assertEqual(self.client.get(reverse('receipts:detail', args=[sha(303)])).json()['total'], '12.00')
        # the list must also survive these rows
        self.assertEqual(self.list_(page_size=200)['count'], 16)

    def test_detail_404_and_odd_keys(self):
        self.assertEqual(self.client.get(reverse('receipts:detail', args=['0' * 64])).status_code, 404)
        self.assertEqual(self.client.get(reverse('receipts:detail', args=['nope'])).status_code, 404)
        self.assertEqual(self.client.get(reverse('receipts:detail', args=["x'--"])).status_code, 404)
        self.assertEqual(self.client.get(reverse('receipts:detail', args=['a' * 300])).status_code, 404)

    def test_view_url_in_detail_matches_list(self):
        s = self.NAMES['a_fy26']
        d = self.client.get(reverse('receipts:detail', args=[s])).json()['view_url']
        l = next(r for r in self.list_(page_size=200)['results'] if r['sha256'] == s)['view_url']
        self.assertEqual(d, l)


# --------------------------------------------------------------------------- #
# 5. Review PATCH
# --------------------------------------------------------------------------- #
class ReviewPatchTests(ReceiptsFixtureMixin, TestCase):
    def url(self, s=None):
        return reverse('receipts:review', args=[s or self.NAMES['a_fy26']])

    def test_unauthenticated_patch_is_401(self):
        resp = self.client.patch(self.url(), {'decision': 'CAPTURE'}, format='json')
        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertFalse(SlipReview.objects.exists())
        # Even for an unknown slip the gate must come before the lookup.
        self.assertEqual(self.client.patch(self.url('0' * 64), {'decision': 'CAPTURE'}, format='json').status_code, 401)

    def test_garbage_bearer_token_is_401(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer not.a.jwt')
        self.assertEqual(self.client.patch(self.url(), {'decision': 'CAPTURE'}, format='json').status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + 'a' * 40)
        self.assertEqual(self.client.patch(self.url(), {'decision': 'CAPTURE'}, format='json').status_code, 401)
        self.assertFalse(SlipReview.objects.exists())

    def test_authenticated_patch_upserts_and_sets_updated_by(self):
        self.auth()
        resp = self.client.patch(self.url(), {'to_process': True, 'decision': 'capture', 'note': 'buy it'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body['decision'], 'CAPTURE')
        self.assertTrue(body['to_process'])
        self.assertEqual(body['note'], 'buy it')
        self.assertEqual(body['updated_by'], 'reviewer')
        self.assertIsNotNone(body['updated_at'])
        rv = SlipReview.objects.get(sha256=self.NAMES['a_fy26'])
        self.assertEqual(rv.updated_by, 'reviewer')
        # second PATCH is an update, not a duplicate; untouched fields persist
        resp = self.client.patch(self.url(), {'note': 'changed'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SlipReview.objects.count(), 1)
        rv.refresh_from_db()
        self.assertEqual(rv.note, 'changed')
        self.assertEqual(rv.decision, 'CAPTURE')
        self.assertTrue(rv.to_process)

    def test_every_valid_decision_including_blank(self):
        self.auth()
        for d in ('', 'CAPTURE', 'MEAL_SKIP', 'PERSONAL', 'DUPLICATE', 'ALREADY_IN_XERO'):
            resp = self.client.patch(self.url(), {'decision': d}, format='json')
            self.assertEqual(resp.status_code, 200, (d, resp.content))
            self.assertEqual(resp.json()['decision'], d)
        # null decision == undecided
        resp = self.client.patch(self.url(), {'decision': None}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['decision'], '')

    def test_invalid_decision_is_400_and_writes_nothing(self):
        self.auth()
        for d in ('BANANA', 'CAPTURE;DROP', 'SKIP', 'MEAL SKIP', 'MEAL-SKIP', 1, ['CAPTURE'], {'x': 1}):
            resp = self.client.patch(self.url(), {'decision': d}, format='json')
            self.assertEqual(resp.status_code, 400, (d, resp.content))
        self.assertFalse(SlipReview.objects.exists())
        # case/whitespace are normalised, not rejected (views.py strip().upper())
        resp = self.client.patch(self.url(), {'decision': ' capture '}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['decision'], 'CAPTURE')

    def test_invalid_decision_with_valid_sibling_field_still_400_atomically(self):
        self.auth()
        resp = self.client.patch(self.url(), {'to_process': True, 'decision': 'NOPE'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(SlipReview.objects.exists(), 'partial write on a 400')

    def test_unknown_sha_is_404(self):
        self.auth()
        self.assertEqual(self.client.patch(self.url('0' * 64), {'decision': 'CAPTURE'}, format='json').status_code, 404)
        self.assertFalse(SlipReview.objects.exists())

    def test_empty_body_is_400(self):
        self.auth()
        self.assertEqual(self.client.patch(self.url(), {}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(self.url()).status_code, 400)
        self.assertEqual(self.client.patch(self.url(), {'unknown_field': 1}, format='json').status_code, 400)
        self.assertFalse(SlipReview.objects.exists())

    def test_to_process_coercions(self):
        self.auth()
        for raw, want in ((True, True), ('true', True), ('1', True), (1, True), ('yes', True), ('on', True),
                          (False, False), ('false', False), ('0', False), (0, False), (None, False)):
            resp = self.client.patch(self.url(), {'to_process': raw}, format='json')
            self.assertEqual(resp.status_code, 200, (raw, resp.content))
            self.assertEqual(resp.json()['to_process'], want, f'to_process={raw!r}')

    @unittest.expectedFailure  # BUG-3 (minor) — remove this decorator once views.py lowercases the value
    def test_to_process_uppercase_TRUE_is_coerced_true(self):
        # BUG-3: views.py receipt_review_view — `data['to_process'] in (True, 1, '1', 'true', 'True', 'yes', 'on')`
        # means 'TRUE' / 'Yes' / 'ON' silently become False: the client is told 200 but the flag is off.
        # Compare with services._bool() which lowercases first; the two coercions disagree.
        self.auth()
        resp = self.client.patch(self.url(), {'to_process': 'TRUE'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['to_process'], "to_process='TRUE' silently coerced to False")

    @unittest.expectedFailure  # BUG-1 — remove this decorator once the view rejects non-dict bodies with 400
    def test_non_object_json_body_does_not_500(self):
        # BUG-1: views.py receipt_review_view does `'note' in data` / `data['note']` on whatever JSON
        # was posted. A JSON *string* body containing the substring 'note' (or 'decision') passes the
        # `in` test and then `data['note']` -> TypeError -> 500; a JSON number body -> TypeError on `in`
        # -> 500; a JSON list ['note'] -> `data['note']` TypeError -> 500. Authenticated caller, but
        # still a 5xx from client input. Fix: `if not isinstance(request.data, dict): return 400`.
        self.auth()
        client = APIClient(raise_request_exception=False)
        client.credentials(**self.client._credentials)
        for raw in ('"a note"', '["note"]', '42', 'null', '"decision"'):
            resp = client.generic('PATCH', self.url(), data=raw, content_type='application/json')
            self.assertIn(resp.status_code, (200, 400), f'body={raw!r} -> {resp.status_code}')
        self.assertFalse(SlipReview.objects.exists())

    def test_note_is_stringified_not_rejected(self):
        self.auth()
        resp = self.client.patch(self.url(), {'note': 123}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['note'], '123')
        resp = self.client.patch(self.url(), {'note': None}, format='json')
        self.assertEqual(resp.json()['note'], '')

    def test_review_visible_in_list_and_export_after_patch(self):
        self.auth()
        self.client.patch(self.url(), {'to_process': True, 'decision': 'DUPLICATE', 'note': 'dup of x'}, format='json')
        row = next(r for r in self.list_(page_size=200)['results'] if r['sha256'] == self.NAMES['a_fy26'])
        self.assertEqual(row['review']['decision'], 'DUPLICATE')
        self.assertTrue(row['review']['to_process'])
        rows = self.csv_rows(self.export(decision='DUPLICATE'))
        self.assertEqual(len(rows), 2)
        hdr = rows[0]
        rec = dict(zip(hdr, rows[1]))
        self.assertEqual(rec['decision'], 'DUPLICATE')
        self.assertEqual(rec['to_process'], 'True')
        self.assertEqual(rec['note'], 'dup of x')

    def test_form_encoded_patch_also_works(self):
        self.auth()
        resp = self.client.patch(self.url(), data='decision=PERSONAL&to_process=true',
                                 content_type='application/x-www-form-urlencoded')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['decision'], 'PERSONAL')
        self.assertTrue(resp.json()['to_process'])


# --------------------------------------------------------------------------- #
# 6. Comment POST
# --------------------------------------------------------------------------- #
class CommentPostTests(ReceiptsFixtureMixin, TestCase):
    def url(self, s=None):
        return reverse('receipts:comments', args=[s or self.NAMES['a_fy26']])

    def test_unauthenticated_post_is_401(self):
        resp = self.client.post(self.url(), {'text': 'hi'}, format='json')
        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertFalse(SlipComment.objects.exists())
        self.assertEqual(self.client.post(self.url('0' * 64), {'text': 'hi'}, format='json').status_code, 401)

    def test_authenticated_post_201_sets_author(self):
        self.auth()
        resp = self.client.post(self.url(), {'text': '  needs VAT invoice  '}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body['text'], 'needs VAT invoice')
        self.assertEqual(body['author'], 'reviewer')
        self.assertIsNotNone(body['created_at'])
        self.assertIn('id', body)
        c = SlipComment.objects.get()
        self.assertEqual(c.author, 'reviewer')
        self.assertEqual(c.sha256, self.NAMES['a_fy26'])

    def test_blank_or_whitespace_text_is_400(self):
        self.auth()
        for t in ('', '   ', '\n\t ', None):
            resp = self.client.post(self.url(), {'text': t}, format='json')
            self.assertEqual(resp.status_code, 400, (t, resp.content))
        self.assertEqual(self.client.post(self.url(), {}, format='json').status_code, 400)
        self.assertEqual(self.client.post(self.url()).status_code, 400)
        self.assertFalse(SlipComment.objects.exists())

    def test_unknown_sha_is_404(self):
        self.auth()
        self.assertEqual(self.client.post(self.url('0' * 64), {'text': 'x'}, format='json').status_code, 404)
        self.assertFalse(SlipComment.objects.exists())

    def test_comment_count_and_detail_order(self):
        self.auth()
        for t in ('one', 'two', 'three'):
            self.assertEqual(self.client.post(self.url(), {'text': t}, format='json').status_code, 201)
        row = next(r for r in self.list_(page_size=200)['results'] if r['sha256'] == self.NAMES['a_fy26'])
        self.assertEqual(row['comment_count'], 3)
        others = [r['comment_count'] for r in self.list_(page_size=200)['results'] if r['sha256'] != self.NAMES['a_fy26']]
        self.assertEqual(set(others), {0})
        detail = self.client.get(reverse('receipts:detail', args=[self.NAMES['a_fy26']])).json()
        self.assertEqual([c['text'] for c in detail['comments']], ['one', 'two', 'three'])
        created = [c['created_at'] for c in detail['comments']]
        self.assertEqual(created, sorted(created))

    @unittest.expectedFailure  # BUG-2 — remove this decorator once the view rejects non-dict bodies with 400
    def test_non_object_json_body_does_not_500(self):
        # BUG-2: views.py receipt_comments_view `(request.data or {}).get('text')` — a non-empty JSON
        # list / number / string body is truthy and has no .get -> AttributeError -> 500.
        # Fix: `if not isinstance(request.data, dict): return 400`.
        self.auth()
        client = APIClient(raise_request_exception=False)
        client.credentials(**self.client._credentials)
        for raw in ('["text"]', '"text"', '42', '[]'):
            resp = client.generic('POST', self.url(), data=raw, content_type='application/json')
            self.assertIn(resp.status_code, (400, 415), f'body={raw!r} -> {resp.status_code}')
        self.assertFalse(SlipComment.objects.exists())

    def test_long_and_unicode_text_is_stored_verbatim(self):
        self.auth()
        text = 'Kwitansie — R1 234,56 — “aanhalings” 🧾 ' + 'x' * 5000
        resp = self.client.post(self.url(), {'text': text}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(SlipComment.objects.get().text, text.strip())

    def test_get_on_comments_url_is_405_not_a_listing(self):
        # unauthenticated GET is gated first (401); authenticated GET must be 405, never a comment dump
        self.assertEqual(self.client.get(self.url()).status_code, 401)
        self.auth()
        self.assertEqual(self.client.get(self.url()).status_code, 405)


# --------------------------------------------------------------------------- #
# 7. Export
# --------------------------------------------------------------------------- #
class ExportTests(ReceiptsFixtureMixin, TestCase):
    EXPECTED_HEADER = ['date', 'supplier', 'total', 'category', 'xero_status', 'status_group', 'journal_number',
                       'synced', 'to_process', 'decision', 'note', 'filename', 'sha256', 'view_url']

    def test_csv_default_headers_and_row_count(self):
        resp = self.export()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp['Content-Type'].startswith('text/csv'), resp['Content-Type'])
        self.assertIn('attachment', resp['Content-Disposition'])
        self.assertRegex(resp['Content-Disposition'], r'filename="receipts-\d{4}-\d{2}-\d{2}\.csv"')
        rows = self.csv_rows(resp)
        self.assertEqual(rows[0], self.EXPECTED_HEADER)
        self.assertEqual(len(rows), 12 + 1)
        self.assertEqual(len({r[12] for r in rows[1:]}), 12, 'duplicate sha rows in export')

    def test_csv_view_url_populated_and_valid(self):
        rows = self.csv_rows(self.export())
        idx = rows[0].index('view_url')
        sidx = rows[0].index('sha256')
        for r in rows[1:]:
            self.assertTrue(r[idx].startswith('http'), r[idx])
            self.assertIn(f'/audit/slip/{r[sidx]}/?s=', r[idx])
            sig = parse_qs(urlparse(r[idx]).query)['s'][0]
            self.assertEqual(sig, slip_signature(r[sidx]))

    def test_csv_respects_filters_and_ordering(self):
        rows = self.csv_rows(self.export(status='MATCHED', ordering='total'))
        self.assertEqual(len(rows), 3 + 1)
        self.assertEqual([r[2] for r in rows[1:]], ['77.00', '259.00', '1234.56'])
        rows = self.csv_rows(self.export(fy='FY26', category='Hardware'))
        self.assertEqual(len(rows), 3 + 1)
        rows = self.csv_rows(self.export(q='nothing-matches-this-zzz'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], self.EXPECTED_HEADER)

    def test_csv_ignores_pagination_params(self):
        rows = self.csv_rows(self.export(page=2, page_size=3))
        self.assertEqual(len(rows), 13, 'export must not be paginated')

    def test_csv_never_contains_file_bytes(self):
        resp = self.export()
        text = resp.content.decode('utf-8', 'replace')
        self.assertFalse(body_has_blob(text))
        self.assertNotIn('file_bytes', text)
        # 13 short CSV lines; a leaked 900-byte blob (or its base64/escaped form) would blow past this.
        self.assertLess(len(resp.content), 13 * 400)

    def test_csv_review_columns(self):
        SlipReview.objects.create(sha256=self.NAMES['a_fy26'], to_process=True, decision='CAPTURE', note='line1\nline2, "quoted"')
        rows = self.csv_rows(self.export(to_process='true'))
        self.assertEqual(len(rows), 2)
        rec = dict(zip(rows[0], rows[1]))
        self.assertEqual(rec['to_process'], 'True')
        self.assertEqual(rec['decision'], 'CAPTURE')
        self.assertEqual(rec['note'], 'line1\nline2, "quoted"')
        self.assertEqual(rec['synced'], 'True')
        self.assertEqual(rec['journal_number'], '697')
        self.assertEqual(rec['status_group'], 'MATCHED')

    def test_csv_null_values(self):
        rows = self.csv_rows(self.export(q='no-timestamp'))
        rec = dict(zip(rows[0], rows[1]))
        self.assertEqual(rec['date'], '')
        self.assertEqual(rec['total'], '15.00')
        self.assertEqual(rec['to_process'], 'False')
        self.assertEqual(rec['decision'], '')

    def test_xlsx_export(self):
        from openpyxl import load_workbook
        resp = self.export(format='xlsx', fy='FY26')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertRegex(resp['Content-Disposition'], r'attachment; filename="receipts-\d{4}-\d{2}-\d{2}\.xlsx"')
        self.assertNotIn('X-Export-Note', resp, 'openpyxl is installed; must not degrade to csv')
        wb = load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        self.assertEqual(ws.title, 'Receipts')
        rows = list(ws.iter_rows(values_only=True))
        self.assertEqual(list(rows[0]), self.EXPECTED_HEADER)
        fy26 = self.list_(fy='FY26', page_size=200)['count']
        self.assertEqual(len(rows) - 1, fy26)
        self.assertEqual(fy26, 9)
        view_idx = self.EXPECTED_HEADER.index('view_url')
        self.assertTrue(all(str(r[view_idx]).startswith('http') for r in rows[1:]))
        # no blob in the zip payload either
        self.assertFalse(body_has_blob(resp.content.decode('latin-1')))

    def test_xlsx_with_blob_row_and_weird_cells(self):
        from openpyxl import load_workbook
        # The blob row + unicode note + newline note must all serialise.
        SlipReview.objects.create(sha256=self.NAMES['c_fy25_pend'], note='ünï\ncode =SUM(1)')
        resp = self.export(format='xlsx')
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        ws = load_workbook(io.BytesIO(resp.content)).active
        self.assertEqual(ws.max_row, 13)

    def test_invalid_format_is_400(self):
        for f in ('pdf', 'json', 'CSV;DROP', 'xls', '../../etc/passwd'):
            resp = self.export(format=f)
            self.assertEqual(resp.status_code, 400, f)
            self.assertEqual(resp['Content-Type'], 'application/json')
        self.assertEqual(self.export(format='CSV').status_code, 200, 'format is case-insensitive')
        self.assertEqual(self.export(format='XLSX').status_code, 200)

    def test_export_rejects_non_get(self):
        self.assertEqual(self.client.post(reverse('receipts:export')).status_code, 405)
        self.assertEqual(self.client.delete(reverse('receipts:export')).status_code, 405)

    def test_export_junk_filters_do_not_500(self):
        resp = self.export(ordering='file_bytes', min_total='NaN', fy='zzz', q="'; drop table x; --", page='x', status='x', decision='y')
        self.assertEqual(resp.status_code, 200)


# --------------------------------------------------------------------------- #
# 8. view_url signature + base URL
# --------------------------------------------------------------------------- #
class ViewUrlTests(ReceiptsFixtureMixin, TestCase):
    def _row(self, name='a_fy26'):
        return next(r for r in self.list_(page_size=200)['results'] if r['sha256'] == self.NAMES[name])

    def test_signature_matches_slip_signature_and_viewer_accepts_it(self):
        from django.conf import settings
        s = self.NAMES['c_fy25_pend']  # the blob row, so the viewer has bytes to serve
        row = self._row('c_fy25_pend')
        sig = slip_signature(s)
        self.assertEqual(len(sig), 32)
        # Exact shape: <SLIP_VIEW_BASE_URL>/audit/slip/<sha>/?s=<sig>  (base may carry the nginx /backend prefix)
        self.assertEqual(row['view_url'], f'{settings.SLIP_VIEW_BASE_URL}/audit/slip/{s}/?s={sig}')
        url = urlparse(row['view_url'])
        self.assertEqual(parse_qs(url.query)['s'][0], sig)
        self.assertTrue(url.path.endswith(f'/audit/slip/{s}/'))
        # Signed viewer honours it (hits the real Django route; the base prefix is nginx's).
        resp = self.client.get(f'/audit/slip/{s}/', {'s': sig})
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.content, BLOB)
        self.assertEqual(resp['Content-Type'], 'image/jpeg')
        self.assertIn('inline', resp['Content-Disposition'])

    def test_tampered_signature_is_rejected(self):
        s = self.NAMES['c_fy25_pend']
        good = slip_signature(s)
        path = f'/audit/slip/{s}/'
        bad = ('0' if good[0] != '0' else '1') + good[1:]
        self.assertEqual(self.client.get(path, {'s': bad}).status_code, 403)
        self.assertEqual(self.client.get(path, {'s': good[:-1]}).status_code, 403)
        self.assertEqual(self.client.get(path, {'s': good + 'a'}).status_code, 403)
        self.assertEqual(self.client.get(path, {'s': ''}).status_code, 403)
        self.assertEqual(self.client.get(path).status_code, 403)
        if good != good.upper():
            self.assertEqual(self.client.get(path, {'s': good.upper()}).status_code, 403)
        # A valid signature for slip X must not open slip Y.
        other = self.NAMES['a_fy26']
        self.assertEqual(self.client.get(f'/audit/slip/{other}/', {'s': good}).status_code, 403)

    def test_signature_for_slip_without_bytes_is_valid_but_404(self):
        s = self.NAMES['a_fy26']  # no file_bytes in fixture
        self.assertEqual(self.client.get(f'/audit/slip/{s}/', {'s': slip_signature(s)}).status_code, 404)

    def test_default_base_url(self):
        with override_settings(SLIP_VIEW_BASE_URL='https://console.8-bit.space/backend'):
            self.assertTrue(self._row()['view_url'].startswith('https://console.8-bit.space/backend/audit/slip/'))

    def test_base_url_honours_setting(self):
        with override_settings(SLIP_VIEW_BASE_URL='https://example.test/xyz'):
            row = self._row()
            self.assertTrue(row['view_url'].startswith('https://example.test/xyz/audit/slip/'), row['view_url'])
            rows = self.csv_rows(self.export(q='Builders'))
            self.assertTrue(all(r[-1].startswith('https://example.test/xyz/') for r in rows[1:]))
            d = self.client.get(reverse('receipts:detail', args=[self.NAMES['a_fy26']])).json()['view_url']
            self.assertTrue(d.startswith('https://example.test/xyz/'))
        with override_settings(SLIP_VIEW_BASE_URL='http://localhost:8000'):
            self.assertTrue(self._row()['view_url'].startswith('http://localhost:8000/audit/slip/'))

    def test_signature_changes_with_secret_key(self):
        s = self.NAMES['a_fy26']
        before = slip_signature(s)
        with override_settings(SECRET_KEY='a-completely-different-secret-key-for-the-test'):
            after = slip_signature(s)
            self.assertNotEqual(before, after)
            self.assertEqual(parse_qs(urlparse(self._row()['view_url']).query)['s'][0], after)


# --------------------------------------------------------------------------- #
# 9. Method / auth matrix on read endpoints (reads are intentionally public)
# --------------------------------------------------------------------------- #
class MethodMatrixTests(ReceiptsFixtureMixin, TestCase):
    def test_reads_are_public_and_writes_are_not(self):
        self.assertEqual(self.client.get(reverse('receipts:list')).status_code, 200)
        self.assertEqual(self.client.get(reverse('receipts:detail', args=[self.NAMES['a_fy26']])).status_code, 200)
        self.assertEqual(self.client.get(reverse('receipts:export')).status_code, 200)
        self.assertEqual(self.client.patch(reverse('receipts:review', args=[self.NAMES['a_fy26']]), {'note': 'x'}, format='json').status_code, 401)
        self.assertEqual(self.client.post(reverse('receipts:comments', args=[self.NAMES['a_fy26']]), {'text': 'x'}, format='json').status_code, 401)

    def test_wrong_methods_are_405_not_writes(self):
        s = self.NAMES['a_fy26']
        self.assertEqual(self.client.post(reverse('receipts:list'), {}, format='json').status_code, 405)
        self.assertEqual(self.client.delete(reverse('receipts:detail', args=[s])).status_code, 405)
        self.assertEqual(self.client.put(reverse('receipts:detail', args=[s]), {}, format='json').status_code, 405)
        self.auth()
        self.assertEqual(self.client.post(reverse('receipts:review', args=[s]), {'note': 'x'}, format='json').status_code, 405)
        self.assertEqual(self.client.put(reverse('receipts:review', args=[s]), {'note': 'x'}, format='json').status_code, 405)
        self.assertEqual(self.client.delete(reverse('receipts:review', args=[s])).status_code, 405)
        self.assertEqual(self.client.patch(reverse('receipts:comments', args=[s]), {'text': 'x'}, format='json').status_code, 405)
        self.assertEqual(self.client.delete(reverse('receipts:comments', args=[s])).status_code, 405)
        self.assertFalse(SlipReview.objects.exists())
        self.assertFalse(SlipComment.objects.exists())

    def test_export_and_list_urls_do_not_collide_with_a_sha_named_export(self):
        # /audit/receipts/export/ must be the export, never the detail of a slip literally named "export"
        insert_slip('export', slip_ts=ts(2025, 1, 1), ocr={'total': '1.00'})
        resp = self.client.get('/audit/receipts/export/')
        self.assertTrue(resp['Content-Type'].startswith('text/csv'))
