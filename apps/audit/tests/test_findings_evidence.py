"""
Adversarial tests for the audit-finding EVIDENCE surfaces: the saved cube view,
linked entities, hardened attachments, and the graph traversal
(apps.audit.findings_cube_views / findings_links_views / findings_graph_views /
findings_views attachments / findings_file_view).

Written against CONTRACT.md (incl. the SENIOR-DEV RULINGS and the R1 reversal —
the detail GET is the ENVELOPE, now also carrying links / link_count /
links_truncated) and CONTRACT-2.md §1–§4. Deliberately written contract-first:
where the contract and the implementation disagree, the FAILING TEST is the
deliverable, not a softened assertion.

Shares the sibling suite's fixtures and temperament (test_findings.py) and does
not duplicate its coverage — the sibling already pins the basic attachment
upload shape, the emitted view_url serving bytes, tampered / missing /
cross-attachment signatures (403), oversize-as-.bin, and the missing ``file``
field. This module owns everything CONTRACT-2 added on top.

Run:  manage.py test apps.audit
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from apps.audit.findings_links_views import KLIKK_TENANT_ID
from apps.audit.models import (
    AuditFinding,
    AuditFindingAttachment,
    AuditFindingLink,
)
from apps.audit.services import fy_bounds
from apps.audit.slip_view import slip_url

# Shared fixture machinery from the sibling suite — reused, never redefined,
# so the two suites cannot drift on FY derivation or auth wiring.
from .test_findings import CUR, FY_A, FY_OLD, AuthedTestBase, attachments_url, mk_finding

# whatsapp.klikk_slips is NOT a Django model (external WhatsApp sync); the
# receipts suite owns the canonical test-DB DDL + row factory for it.
from apps.receipts.tests import create_slips_table, insert_slip

UTC = dt.timezone.utc

GRAPH_URL = '/audit/findings/graph/'

# The contract's Klikk tenant uuid, hardcoded on purpose (the canonicalised
# ref format is API surface — "171" must be STORED as "<this uuid>:171").
KLIKK_UUID = '41ebfa0e-012e-4ff1-82ba-a9a7585c536c'
TREMLY_UUID = '9e07b7d4-5a3e-4b6f-8c2d-1f2e3a4b5c6d'

TEMP_MEDIA = tempfile.mkdtemp(prefix='test-audit-evidence-media-')


def cube_url(pk):
    return f'/audit/findings/{pk}/cube-view/'


def cube_data_url(pk):
    return f'/audit/findings/{pk}/cube-view/data/'


def cube_suggest_url(pk):
    return f'/audit/findings/{pk}/cube-view/suggest/'


def links_url(pk):
    return f'/audit/findings/{pk}/links/'


def link_delete_url(link_id):
    return f'/audit/findings/links/{link_id}/'


def attachment_delete_url(att_id):
    return f'/audit/findings/attachments/{att_id}/'


def sha(n: int) -> str:
    """Deterministic 64-hex sha256-shaped key (same convention as the receipts suite)."""
    return f'{n:064x}'


VALID_SPEC = {
    'rows': ['supplier'],
    'cols': ['fin_period'],
    'measure': 'amount',
    'filters': {'fin_year': [f'FY{FY_A}']},
    'totals': {},
    'suppress': True,
    'outline': True,
}


def edge_key(e: dict) -> tuple:
    return (e['from_type'], e['from_id'], e['edge'], e['to_type'], e['to_id'])


# --------------------------------------------------------------------------- #
# Cross-app fixture builders (Xero / Investec mirrors). XeroAccount/XeroContacts
# go through bulk_create to skip the pre-existing glossary post_save bug — the
# same workaround, for the same reason, as apps/receipts/tests.py.
# --------------------------------------------------------------------------- #
def make_tenant(tenant_id, name):
    from apps.xero.xero_core.models import XeroTenant
    return XeroTenant.objects.create(
        tenant_id=tenant_id, tenant_name=name, fiscal_year_start_month=7)


def make_account(tenant, account_id):
    from apps.xero.xero_metadata.models import XeroAccount
    return XeroAccount.objects.bulk_create(
        [XeroAccount(organisation=tenant, account_id=account_id, code='4000',
                     name='Expenses', type='EXPENSE')])[0]


def make_contact(tenant, contacts_id, name):
    from apps.xero.xero_metadata.models import XeroContacts
    return XeroContacts.objects.bulk_create(
        [XeroContacts(organisation=tenant, contacts_id=contacts_id, name=name)])[0]


def make_journal_line(tenant, account, number, amount, description, *, journal_id,
                      when=None):
    from apps.xero.xero_data.models import XeroJournals
    amount = Decimal(amount)
    return XeroJournals.objects.create(
        organisation=tenant, journal_id=journal_id, journal_number=number,
        journal_type='transaction', account=account,
        date=when or dt.datetime(FY_A, 3, 1, tzinfo=UTC),
        description=description, amount=amount,
        debit=amount if amount > 0 else Decimal('0'),
        credit=amount if amount < 0 else Decimal('0'),
        tax_amount=Decimal('0'),
    )


def make_invoice(tenant, number, contact_name, total, *, invoice_id, when=None):
    from apps.xero.xero_data.models import XeroInvoice
    return XeroInvoice.objects.create(
        organisation=tenant, invoice_id=invoice_id, invoice_number=number,
        type='ACCPAY', status='AUTHORISED', contact_name=contact_name,
        date=when or dt.date(FY_A, 3, 10), total=Decimal(total),
    )


def make_document(tenant, contact, file_name, *, transactions_id):
    from apps.xero.xero_data.models import XeroDocument, XeroTransactionSource
    source = XeroTransactionSource.objects.create(
        organisation=tenant, transactions_id=transactions_id,
        transaction_source='ACCPAY', contact=contact)
    return XeroDocument.objects.create(
        organisation=tenant, transaction_source=source, file_name=file_name)


def make_bank_txn(*, amount, when, description, uuid=None, txn_type='DEBIT'):
    from apps.investec.models import InvestecBankAccount, InvestecBankTransaction
    account, _ = InvestecBankAccount.objects.get_or_create(
        account_id='acc-test-1',
        defaults={'account_number': '10012345678', 'account_name': 'Klikk Current'})
    return InvestecBankTransaction.objects.create(
        account=account, type=txn_type, status='POSTED', description=description,
        amount=Decimal(amount), transaction_date=when, uuid=uuid,
    )


# =========================================================================== #
# 1. Auth gate — 401 (not 403) on every new endpoint; the signed viewer is the
#    ONE deliberate exception and 403s on a bad signature instead of 401ing.
# =========================================================================== #
class EvidenceAuthGateTests(AuthedTestBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.f = mk_finding(FY_A, 1, 'auth-gate probe finding')

    def test_anonymous_is_401_on_every_new_endpoint(self):
        pk = self.f.pk
        probes = [
            ('put', cube_url(pk)),
            ('delete', cube_url(pk)),
            ('get', cube_data_url(pk)),
            ('get', cube_suggest_url(pk)),
            ('get', links_url(pk)),
            ('post', links_url(pk)),
            ('delete', link_delete_url(999)),
            ('get', attachments_url(pk)),
            ('post', attachments_url(pk)),
            ('delete', attachment_delete_url(999)),
            ('get', GRAPH_URL),
        ]
        for method, url in probes:
            resp = getattr(self.anon, method)(url)
            self.assertEqual(
                resp.status_code, 401,
                f'{method.upper()} {url} -> {resp.status_code}, expected 401 for anonymous')
            self.assertIn('Bearer', resp.headers.get('WWW-Authenticate', ''),
                          f'{method.upper()} {url} 401 must carry the Bearer challenge')

    def test_signed_viewer_is_public_but_403s_on_bad_or_missing_signature(self):
        # No auth in sight — the HMAC is the only guard, so the failure mode is
        # 403 (bad credential for THIS resource), never 401 (no credential).
        url = '/audit/findings/attachments/424242/file/'
        self.assertEqual(self.anon.get(url).status_code, 403)
        self.assertEqual(self.anon.get(url, {'s': 'f' * 32}).status_code, 403)


# =========================================================================== #
# 2. Cube view — lifecycle
# =========================================================================== #
class CubeViewLifecycleTests(AuthedTestBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.f = mk_finding(FY_A, 1, 'cube lifecycle finding')

    def put_cube(self, body=None, pk=None):
        payload = {'spec': VALID_SPEC, 'query': {'journal_type': 'transaction'},
                   'name': 'By supplier', 'cube_note': 'why this slice matters'}
        if body is not None:
            payload = body
        return self.client.put(cube_url(pk or self.f.pk), payload, format='json')

    def test_put_saves_the_canonical_shape(self):
        resp = self.put_cube()
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        self.f.refresh_from_db()
        self.assertIsNotNone(self.f.cube_view)
        self.assertEqual(self.f.cube_view['spec']['rows'], ['supplier'])
        self.assertEqual(self.f.cube_view['spec']['cols'], ['fin_period'])
        self.assertEqual(self.f.cube_view['spec']['measure'], 'amount')
        self.assertEqual(self.f.cube_view['name'], 'By supplier')
        self.assertEqual(self.f.cube_note, 'why this slice matters')

    def test_data_returns_the_cross_tab_envelope_with_faithful_params(self):
        self.put_cube()
        resp = self.client.get(cube_data_url(self.f.pk))
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        body = resp.json()
        for key in ('finding_id', 'fy', 'name', 'spec', 'query', 'params', 'cube'):
            self.assertIn(key, body)
        self.assertEqual(body['finding_id'], self.f.pk)
        self.assertEqual(body['fy'], FY_A)
        params = body['params']
        # The server-side twin of cubeQueryParams(): same wire shape the Excel
        # add-in sends, so the two surfaces can never disagree on the numbers.
        self.assertEqual(params['rows'], 'supplier')
        self.assertEqual(params['cols'], 'fin_period')
        self.assertEqual(params['measure'], 'amount')
        self.assertEqual(params['suppress'], '1')
        self.assertEqual(json.loads(params['dimf']), {'fin_year': [f'FY{FY_A}']})
        self.assertEqual(params['journal_type'], 'transaction')
        self.assertIsInstance(body['cube'], dict)

    def test_delete_clears_the_view_AND_the_note(self):
        # Senior ruling, pinned: the note annotates the view; they live and die
        # together. A surviving cube_note would describe a cube that no longer
        # exists — worse than no note at all.
        self.put_cube()
        self.f.refresh_from_db()
        self.assertTrue(self.f.cube_note)
        resp = self.client.delete(cube_url(self.f.pk))
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        self.f.refresh_from_db()
        self.assertIsNone(self.f.cube_view, 'DELETE must clear cube_view')
        self.assertEqual(self.f.cube_note, '',
                         'SENIOR RULING: DELETE must clear cube_note along with the view')

    def test_data_with_nothing_saved_is_404_with_a_detail_not_a_500(self):
        self.assertIsNone(AuditFinding.objects.get(pk=self.f.pk).cube_view)
        resp = self.client.get(cube_data_url(self.f.pk))
        self.assertEqual(resp.status_code, 404, resp.content[:500])
        self.assertEqual(resp.json().get('detail'), 'no cube view saved on this finding')

    def test_data_after_delete_is_404_again(self):
        self.put_cube()
        self.client.delete(cube_url(self.f.pk))
        resp = self.client.get(cube_data_url(self.f.pk))
        self.assertEqual(resp.status_code, 404)
        self.assertIn('detail', resp.json())

    def test_unknown_finding_is_404_on_all_cube_routes(self):
        for method, url in (('put', cube_url(999999)),
                            ('delete', cube_url(999999)),
                            ('get', cube_data_url(999999)),
                            ('get', cube_suggest_url(999999))):
            kwargs = {'data': {'spec': VALID_SPEC}, 'format': 'json'} if method == 'put' else {}
            resp = getattr(self.client, method)(url, **kwargs)
            self.assertEqual(resp.status_code, 404, f'{method.upper()} {url}')


# =========================================================================== #
# 3. Cube view — validation 400s and the deliberate error-key split
# =========================================================================== #
class CubeViewValidationTests(AuthedTestBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.f = mk_finding(FY_A, 1, 'cube validation finding')

    def put_spec(self, spec, **extra):
        body = {'spec': spec}
        body.update(extra)
        return self.client.put(cube_url(self.f.pk), body, format='json')

    def assert_own_validation_400(self, resp, fragment=''):
        """The endpoint's OWN validation speaks {'detail': ...} — never 'error'."""
        self.assertEqual(resp.status_code, 400, resp.content[:500])
        body = resp.json()
        self.assertIn('detail', body,
                      "the cube endpoint's own validation must use the 'detail' key")
        self.assertNotIn('error', body,
                         "'error' is reserved for the pivot's verbatim pass-through")
        if fragment:
            self.assertIn(fragment, body['detail'])
        # A rejected PUT must not have half-saved anything.
        self.f.refresh_from_db()
        self.assertIsNone(self.f.cube_view)

    def test_unknown_dimension_is_400(self):
        self.assert_own_validation_400(
            self.put_spec({'rows': ['not_a_dimension'], 'cols': []}), 'unknown dimension')

    def test_dimension_on_both_axes_is_400(self):
        self.assert_own_validation_400(
            self.put_spec({'rows': ['supplier'], 'cols': ['supplier']}), 'both axes')

    def test_unknown_measure_is_400(self):
        self.assert_own_validation_400(
            self.put_spec({'rows': ['supplier'], 'measure': 'profit'}), 'unknown measure')

    def test_empty_rows_is_400(self):
        self.assert_own_validation_400(self.put_spec({'rows': [], 'cols': ['month']}))

    def test_rows_not_a_list_is_400(self):
        self.assert_own_validation_400(self.put_spec({'rows': 'supplier'}))
        self.assert_own_validation_400(self.put_spec({'rows': {'supplier': 1}}))

    def test_spec_not_an_object_is_400(self):
        for bad_spec in ('supplier by month', 42, ['supplier'], None):
            self.assert_own_validation_400(self.put_spec(bad_spec))

    def test_body_not_an_object_is_400(self):
        resp = self.client.put(cube_url(self.f.pk), ['not', 'an', 'object'], format='json')
        self.assert_own_validation_400(resp)

    def test_a_spec_with_200_dimensions_is_400_never_500(self):
        monster = {'rows': [f'dim_{i}' for i in range(200)], 'cols': []}
        self.assert_own_validation_400(self.put_spec(monster), 'unknown dimension')

    def test_unknown_dimension_inside_filters_is_400(self):
        self.assert_own_validation_400(
            self.put_spec({'rows': ['supplier'], 'filters': {'bogus': ['x']}}),
            'unknown dimension')

    def test_nul_in_a_filter_value_is_400_never_500(self):
        resp = self.put_spec({'rows': ['supplier'],
                              'filters': {'fin_year': ['FY\x002026']}})
        self.assertEqual(resp.status_code, 400, resp.content[:500])
        self.assertIn('detail', resp.json())

    def test_pivot_errors_pass_through_verbatim_with_the_pivot_error_key(self):
        # The other half of the deliberate split: an error raised by the
        # UNDERLYING pivot travels through /data/ untouched, native
        # {'error': ...} shape and all. Save a stale spec straight onto the row
        # (as if the dimension vocabulary shrank after the view was saved).
        self.f.cube_view = {
            'name': None,
            'spec': {'rows': ['dimension_that_no_longer_exists'], 'cols': [],
                     'measure': 'amount', 'filt': [], 'filters': {}, 'totals': {},
                     'suppress': True, 'outline': True},
            'query': {},
        }
        self.f.save(update_fields=['cube_view'])
        resp = self.client.get(cube_data_url(self.f.pk))
        self.assertEqual(resp.status_code, 400, resp.content[:500])
        body = resp.json()
        self.assertIn('error', body,
                      "a pivot rejection must pass through VERBATIM as the pivot's "
                      "native {'error': ...} shape")
        self.assertNotIn('detail', body)
        self.assertIn('unknown dimension', body['error'])


# =========================================================================== #
# 4. Cube view — /suggest/ derives from structured data only, never saves
# =========================================================================== #
class CubeSuggestTests(AuthedTestBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        create_slips_table()
        cls.klikk = make_tenant(KLIKK_UUID, 'Klikk (Pty) Ltd')
        cls.contact = make_contact(cls.klikk, 'contact-aurras', 'Aurras Trading')
        cls.doc = make_document(cls.klikk, cls.contact, 'aurras-statement-oct.pdf',
                                transactions_id='txn-src-doc-1')
        cls.bank_txn = make_bank_txn(amount='-8350.00', when=dt.date(FY_A, 3, 15),
                                     description='AURRAS EFT', uuid='txn-uuid-sg1')

    def setUp(self):
        super().setUp()
        # Fresh finding per test: suggest must be provably side-effect free.
        self.f = mk_finding(
            FY_A, AuditFinding.objects.filter(fy=FY_A).count() + 1,
            'Builders Warehouse duplicate — R138,000.00 charged twice, Aurras unpaid',
            description='Prose mentions Aurras and R99,810 — none of it is structured data.')

    def suggest(self, finding=None):
        resp = self.client.get(cube_suggest_url((finding or self.f).pk))
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        return resp.json()

    def test_suggest_never_saves_and_returns_derived_from(self):
        before = AuditFinding.objects.get(pk=self.f.pk)
        self.assertIsNone(before.cube_view)
        body = self.suggest()
        self.assertIn('derived_from', body)
        self.assertIn('fy', body['derived_from'])
        after = AuditFinding.objects.get(pk=self.f.pk)
        self.assertIsNone(after.cube_view, '/suggest/ must NOT save anything')
        self.assertEqual(after.cube_note, '')
        self.assertEqual(after.updated_at, before.updated_at,
                         '/suggest/ must not even touch the row')

    def test_suggest_never_invents_a_supplier_or_amount_from_prose(self):
        # The title is stuffed with a supplier name and rand amounts. If ANY of
        # it leaks into the spec, the console would put invented numbers in
        # front of an auditor.
        body = self.suggest()
        dump = json.dumps(body['spec']) + json.dumps(body['query'])
        for leaked in ('Builders', 'Aurras', '138000', '138,000', '99810', '99,810'):
            self.assertNotIn(leaked, dump,
                             f'{leaked!r} from the finding prose leaked into the suggested spec')

    def test_fy_only_default_layout_and_date_range(self):
        body = self.suggest()
        start, end = fy_bounds(FY_A)
        self.assertEqual(body['spec']['rows'], ['supplier'])
        self.assertEqual(body['spec']['cols'], ['fin_period'])
        self.assertEqual(body['spec']['measure'], 'amount')
        self.assertEqual(body['spec']['filters'].get('fin_year'), [f'FY{FY_A}'])
        self.assertEqual(body['query'].get('journal_type'), 'transaction')
        self.assertEqual(body['query'].get('date_from'), start.isoformat())
        self.assertEqual(body['query'].get('date_to'), end.isoformat())

    def test_journal_link_scopes_by_entity_and_date_never_by_a_text_search_for_the_number(self):
        # SENIOR-DEV RULING, superseding CONTRACT-2 §2's "query.q = <journal_number>".
        #
        # The original code put the tenant-qualified ref ("<uuid>:171") into q, which
        # could never match — that was DEFECT 2 and it is fixed. But the specified
        # fallback (put the BARE number in q) is not safe either: the pivot has NO
        # journal_number filter, and its q is an icontains across description /
        # reference / contact name / account code+name / tenant name. A q of "171"
        # therefore matches account code 1710, a reference containing 171, a contact
        # named "171 Main Rd" — and returns those rows to an auditor labelled as though
        # the cube were scoped to journal 171. Silently-wrong scope is worse than
        # absent scope in an audit register, so the NUMBER IS DROPPED.
        #
        # What is kept is what the pivot can honestly honour, both from structured data:
        # the tenant half (a real pivot filter) and the journal's own date window.
        # derived_from states plainly that the number could not be used.
        resp = self.client.post(links_url(self.f.pk),
                                {'kind': 'journal', 'ref': '171'}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        body = self.suggest()
        q = body['query'].get('q')

        # The qualified ref must never leak into q (the original defect).
        self.assertNotIn('171', str(q or ''),
                         f'the journal number must not be smuggled into q: {q!r}')
        self.assertIsNone(
            None if q in (None, '') else q,
            f'q must be unset for a journal link — the pivot cannot filter on a journal '
            f'number and a text match on it returns unrelated rows. Got: {q!r}')

        # The tenant half IS a real pivot filter and must be applied.
        self.assertEqual(body['query'].get('tenant'), KLIKK_TENANT_ID,
                         f"the journal's entity must scope the cube: {body['query']!r}")

        # derived_from must name the journal link AND admit the number was not usable.
        joined = ' | '.join(body['derived_from'])
        self.assertIn('journal', joined,
                      f'derived_from must name the journal link: {joined}')
        self.assertTrue(
            'cannot filter by journal number' in joined,
            f'derived_from must say the journal number could not be applied, so nobody '
            f'reads this cube as scoped to one journal: {joined}')

    def test_document_link_seeds_the_supplier_filter_from_the_resolved_contact(self):
        self.client.post(links_url(self.f.pk),
                         {'kind': 'xero_document', 'ref': str(self.doc.id)}, format='json')
        body = self.suggest()
        self.assertEqual(body['spec']['filters'].get('supplier'), ['Aurras Trading'])
        self.assertTrue(any('xero_document' in d for d in body['derived_from']))

    def test_bank_transaction_link_narrows_the_date_range(self):
        self.client.post(links_url(self.f.pk),
                         {'kind': 'bank_transaction', 'ref': str(self.bank_txn.id)},
                         format='json')
        body = self.suggest()
        want = dt.date(FY_A, 3, 15).isoformat()
        self.assertEqual(body['query'].get('date_from'), want)
        self.assertEqual(body['query'].get('date_to'), want)
        self.assertTrue(any('bank_transaction' in d for d in body['derived_from']))


# =========================================================================== #
# 5. Links — validation and idempotency (incl. the tenant-qualification trap)
# =========================================================================== #
class LinkValidationTests(AuthedTestBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.f = mk_finding(FY_A, 1, 'link validation finding')

    def post_link(self, body):
        return self.client.post(links_url(self.f.pk), body, format='json')

    def test_unknown_kind_is_400_naming_the_kinds(self):
        resp = self.post_link({'kind': 'carrier_pigeon', 'ref': 'x'})
        self.assertEqual(resp.status_code, 400, resp.content[:300])
        detail = resp.json()['detail']
        for kind in ('slip', 'xero_document', 'bank_transaction', 'journal',
                     'invoice', 'asana'):
            self.assertIn(kind, detail, f'400 must name the valid kinds; missing {kind}')

    def test_blank_and_whitespace_ref_is_400(self):
        for ref in ('', '   ', '\t\n'):
            resp = self.post_link({'kind': 'slip', 'ref': ref})
            self.assertEqual(resp.status_code, 400, f'ref={ref!r}')

    def test_nul_in_ref_is_400_never_500(self):
        resp = self.post_link({'kind': 'slip', 'ref': 'abc\x00def'})
        self.assertEqual(resp.status_code, 400, resp.content[:300])
        self.assertEqual(AuditFindingLink.objects.count(), 0)

    def test_non_object_body_is_400(self):
        resp = self.client.post(links_url(self.f.pk), ['kind', 'slip'], format='json')
        self.assertEqual(resp.status_code, 400)

    def test_unknown_finding_is_404(self):
        resp = self.client.post(links_url(999999), {'kind': 'slip', 'ref': sha(1)},
                                format='json')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.client.get(links_url(999999)).status_code, 404)

    def test_delete_unknown_link_is_404_and_delete_removes_the_row(self):
        created = self.post_link({'kind': 'asana', 'ref': 'gid-1'}).json()['link']
        self.assertEqual(self.client.delete(link_delete_url(999999)).status_code, 404)
        resp = self.client.delete(link_delete_url(created['id']))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AuditFindingLink.objects.filter(pk=created['id']).exists())


class LinkIdempotencyTests(AuthedTestBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        create_slips_table()
        cls.f = mk_finding(FY_A, 1, 'link idempotency finding')

    def post_link(self, kind, ref, **extra):
        body = {'kind': kind, 'ref': ref}
        body.update(extra)
        return self.client.post(links_url(self.f.pk), body, format='json')

    def test_duplicate_post_returns_200_created_false_and_one_row(self):
        first = self.post_link('slip', sha(7))
        self.assertEqual(first.status_code, 201, first.content[:300])
        self.assertIs(first.json()['created'], True)
        second = self.post_link('slip', sha(7))
        self.assertEqual(second.status_code, 200,
                         'a duplicate is idempotent: 200, not 409 and not 201')
        self.assertIs(second.json()['created'], False)
        self.assertEqual(second.json()['link']['id'], first.json()['link']['id'])
        self.assertEqual(
            AuditFindingLink.objects.filter(finding=self.f, kind='slip').count(), 1)

    def test_bare_journal_ref_is_stored_canonicalised_to_the_klikk_tenant(self):
        resp = self.post_link('journal', '171')
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        stored = AuditFindingLink.objects.get(finding=self.f, kind='journal')
        self.assertEqual(stored.ref, f'{KLIKK_UUID}:171',
                         'a bare journal number must be stored tenant-qualified')
        self.assertEqual(resp.json()['link']['ref'], f'{KLIKK_UUID}:171')

    def test_bare_then_klikk_qualified_is_ONE_link_not_two(self):
        # The idempotency trap: "171" and "<klikk-uuid>:171" are the same journal.
        self.post_link('journal', '171')
        resp = self.post_link('journal', f'{KLIKK_UUID}:171')
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertIs(resp.json()['created'], False)
        self.assertEqual(
            AuditFindingLink.objects.filter(finding=self.f, kind='journal').count(), 1,
            'bare and Klikk-qualified forms of one journal must collapse to ONE link')

    def test_uppercase_qualified_uuid_still_collapses_to_the_same_link(self):
        self.post_link('journal', '171')
        resp = self.post_link('journal', f'{KLIKK_UUID.upper()}:171')
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(
            AuditFindingLink.objects.filter(finding=self.f, kind='journal').count(), 1)

    def test_a_different_tenant_is_a_DISTINCT_link(self):
        self.post_link('journal', '171')
        resp = self.post_link('journal', f'{TREMLY_UUID}:171')
        self.assertEqual(resp.status_code, 201,
                         'the same number in another tenant is a different journal')
        self.assertEqual(
            AuditFindingLink.objects.filter(finding=self.f, kind='journal').count(), 2)

    def test_invoice_refs_are_qualified_too(self):
        resp = self.post_link('invoice', 'INV-171')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(
            AuditFindingLink.objects.get(finding=self.f, kind='invoice').ref,
            f'{KLIKK_UUID}:INV-171')
        dup = self.post_link('invoice', f'{KLIKK_UUID}:INV-171')
        self.assertEqual(dup.status_code, 200)
        self.assertIs(dup.json()['created'], False)

    def test_slip_and_asana_refs_are_NOT_qualified(self):
        self.post_link('slip', sha(9))
        self.post_link('asana', '1217633700114593')
        for kind, ref in (('slip', sha(9)), ('asana', '1217633700114593')):
            self.assertEqual(
                AuditFindingLink.objects.get(finding=self.f, kind=kind).ref, ref,
                f'{kind} refs pass through verbatim — only journal/invoice are qualified')


# =========================================================================== #
# 6. Links — resolution (right tenant, dangling degrade, batching)
# =========================================================================== #
class LinkResolutionTests(AuthedTestBase):
    SLIP_SHA = sha(21)

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        create_slips_table()
        cls.f = mk_finding(FY_A, 1, 'link resolution finding')

        insert_slip(cls.SLIP_SHA, slip_ts=dt.datetime(FY_A, 3, 11, 10, 0, tzinfo=UTC),
                    ocr={'supplier': 'Builders Warehouse', 'total': '1204.50'})

        cls.klikk = make_tenant(KLIKK_UUID, 'Klikk (Pty) Ltd')
        cls.tremly = make_tenant(TREMLY_UUID, 'Tremly (Pty) Ltd')
        klikk_acc = make_account(cls.klikk, 'acc-klikk-1')
        tremly_acc = make_account(cls.tremly, 'acc-tremly-1')
        # Journal number 171 exists in BOTH tenants — the whole reason refs are
        # qualified. Distinct narrations so a wrong-tenant resolution is visible.
        make_journal_line(cls.klikk, klikk_acc, 171, '100.00',
                          'Klikk narration for 171', journal_id='jid-klikk-171-a')
        make_journal_line(cls.klikk, klikk_acc, 171, '250.00',
                          'Klikk narration for 171', journal_id='jid-klikk-171-b')
        make_journal_line(cls.tremly, tremly_acc, 171, '999.00',
                          'Tremly narration for 171', journal_id='jid-tremly-171')

        make_invoice(cls.klikk, 'INV-171', 'Aurras Trading', '138000.00',
                     invoice_id='inv-klikk-171')

        contact = make_contact(cls.klikk, 'contact-bw', 'Builders Warehouse')
        cls.doc = make_document(cls.klikk, contact, 'bw-invoice-mar.pdf',
                                transactions_id='txn-src-res-1')
        cls.bank_txn = make_bank_txn(amount='-8350.00', when=dt.date(FY_A, 3, 15),
                                     description='RENT RECEIVED BRIT',
                                     uuid='txn-uuid-res1', txn_type='CREDIT')

    def add_link(self, kind, ref):
        resp = self.client.post(links_url(self.f.pk), {'kind': kind, 'ref': ref},
                                format='json')
        self.assertIn(resp.status_code, (200, 201), resp.content[:300])
        return resp.json()['link']

    def get_links(self):
        resp = self.client.get(links_url(self.f.pk))
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        return resp.json()

    def by_ref(self, body, ref_fragment, kind=None):
        return next(l for l in body['links']
                    if ref_fragment in l['ref'] and (kind is None or l['kind'] == kind))

    def test_slip_resolves_with_title_subtitle_and_the_signed_slip_url(self):
        self.add_link('slip', self.SLIP_SHA)
        link = self.by_ref(self.get_links(), self.SLIP_SHA)
        res = link['resolved']
        self.assertIs(res['found'], True)
        self.assertEqual(res['title'], 'Builders Warehouse R1,204.50')
        self.assertEqual(res['subtitle'], dt.date(FY_A, 3, 11).isoformat())
        self.assertEqual(res['view_url'], slip_url(self.SLIP_SHA))

    def test_tremly_qualified_journal_resolves_to_the_tremly_row_not_the_klikk_one(self):
        # This is the whole point of qualifying — make it bite.
        self.add_link('journal', f'{TREMLY_UUID}:171')
        link = self.by_ref(self.get_links(), TREMLY_UUID)
        res = link['resolved']
        self.assertIs(res['found'], True)
        self.assertEqual(res['detail']['tenant_id'], TREMLY_UUID)
        self.assertEqual(res['title'], 'Tremly narration for 171',
                         "a Tremly-qualified ref resolved to the WRONG tenant's journal")
        self.assertNotIn('Klikk', res['title'])
        self.assertEqual(res['detail']['line_count'], 1)

    def test_bare_journal_ref_resolves_to_the_klikk_rows(self):
        self.add_link('journal', '171')
        link = self.by_ref(self.get_links(), KLIKK_UUID)
        res = link['resolved']
        self.assertIs(res['found'], True)
        self.assertEqual(res['detail']['tenant_id'], KLIKK_UUID)
        self.assertEqual(res['title'], 'Klikk narration for 171')
        self.assertEqual(res['detail']['line_count'], 2)

    def test_invoice_document_and_bank_links_resolve(self):
        self.add_link('invoice', 'INV-171')
        self.add_link('xero_document', str(self.doc.id))
        self.add_link('bank_transaction', str(self.bank_txn.id))
        self.add_link('bank_transaction', 'txn-uuid-res1')  # distinct link, same txn by uuid
        body = self.get_links()

        inv = self.by_ref(body, 'INV-171', kind='invoice')['resolved']
        self.assertIs(inv['found'], True)
        self.assertIn('INV-171', inv['title'])
        self.assertIn('Aurras Trading', inv['title'])

        doc = self.by_ref(body, str(self.doc.id), kind='xero_document')['resolved']
        self.assertIs(doc['found'], True)
        self.assertEqual(doc['title'], 'bw-invoice-mar.pdf')
        self.assertTrue(doc['view_url'])

        bank_links = [l for l in body['links'] if l['kind'] == 'bank_transaction']
        self.assertEqual(len(bank_links), 2, 'pk-ref and uuid-ref are distinct links')
        for l in bank_links:
            res = l['resolved']
            self.assertIs(res['found'], True, f"bank ref {l['ref']!r} did not resolve")
            self.assertEqual(res['detail']['transaction_date'],
                             dt.date(FY_A, 3, 15).isoformat())
            self.assertEqual(res['detail']['amount'], '-8350.00',
                             'credits keep their stored negative sign')
            self.assertIsNone(res['view_url'], 'there is no file behind a bank txn')

    def test_asana_links_resolve_locally_without_any_lookup(self):
        self.add_link('asana', '1217633700114593')
        res = self.by_ref(self.get_links(), '1217633700114593')['resolved']
        self.assertIs(res['found'], True)
        self.assertEqual(res['view_url'], 'https://app.asana.com/0/0/1217633700114593')

    def test_dangling_refs_degrade_to_found_false_with_a_200_never_an_error(self):
        dangling = [
            ('slip', sha(99)),                      # a purged slip
            ('xero_document', '999999'),            # nonexistent document id
            ('bank_transaction', 'no-such-uuid-1'), # bogus bank txn id
            ('journal', '999171'),                  # number that exists in no org
            ('invoice', 'INV-NOPE'),
        ]
        for kind, ref in dangling:
            self.add_link(kind, ref)
        body = self.get_links()
        for kind, ref in dangling:
            fragment = ref.split(':')[-1]
            link = next(l for l in body['links']
                        if l['kind'] == kind and fragment in l['ref'])
            self.assertIs(link['resolved']['found'], False,
                          f'dangling {kind} ref must come back found:false')
            self.assertTrue(link['resolved']['title'],
                            'a dangling ref is still rendered, titled with its raw ref')

    def test_resolution_is_O_kinds_not_O_links(self):
        # Same kinds present, 6 links vs 30 links: the query count must be
        # IDENTICAL, and small in absolute terms. Dangling refs are used for
        # both sides so each per-kind resolver takes the same code path.
        f_small = mk_finding(FY_A, 2, 'batching probe — small')
        f_large = mk_finding(FY_A, 3, 'batching probe — large')

        def seed(finding, per_kind):
            rows = []
            for i in range(per_kind):
                rows += [
                    AuditFindingLink(finding=finding, kind='slip', ref=sha(500 + i)),
                    AuditFindingLink(finding=finding, kind='xero_document',
                                     ref=str(880000 + i)),
                    AuditFindingLink(finding=finding, kind='bank_transaction',
                                     ref=str(770000 + i)),
                    AuditFindingLink(finding=finding, kind='journal',
                                     ref=f'{KLIKK_UUID}:{660000 + i}'),
                    AuditFindingLink(finding=finding, kind='invoice',
                                     ref=f'{KLIKK_UUID}:INV-B{i}'),
                    AuditFindingLink(finding=finding, kind='asana', ref=f'gid-b{i}'),
                ]
            AuditFindingLink.objects.bulk_create(rows)

        seed(f_small, 1)
        seed(f_large, 5)

        with CaptureQueriesContext(connection) as ctx_small:
            resp = self.client.get(links_url(f_small.pk))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()['count'], 6)
        with CaptureQueriesContext(connection) as ctx_large:
            resp = self.client.get(links_url(f_large.pk))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()['count'], 30)

        self.assertEqual(
            len(ctx_small), len(ctx_large),
            f'query count grew with link count ({len(ctx_small)} -> {len(ctx_large)}): '
            'resolution must be one batch per KIND, not per link')
        self.assertLessEqual(len(ctx_large), 18,
                             f'{len(ctx_large)} queries to resolve one finding\'s links '
                             'is not O(kinds)')


# =========================================================================== #
# 7. Links — the list dict's link_count and the detail payload's 200-link cap
# =========================================================================== #
class LinkCountAndDetailCapTests(AuthedTestBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.f = mk_finding(FY_A, 1, 'link count finding')

    def test_list_dict_carries_link_count(self):
        # CONTRACT-2 §3: the LIST dict gains link_count alongside
        # comment_count / attachment_count.
        AuditFindingLink.objects.bulk_create([
            AuditFindingLink(finding=self.f, kind='asana', ref=f'gid-lc{i}')
            for i in range(3)
        ])
        data = self.client.get('/audit/findings/', {'fy': str(FY_A)}).json()
        row = next(r for r in data['results'] if r['id'] == self.f.pk)
        self.assertIn(
            'link_count', row,
            "CONTRACT-2 §3 VIOLATION: the list dict must carry 'link_count' alongside "
            "comment_count/attachment_count")
        self.assertEqual(row['link_count'], 3)

    def test_detail_envelope_caps_links_at_200_with_true_count_and_flag(self):
        AuditFindingLink.objects.bulk_create([
            AuditFindingLink(finding=self.f, kind='asana', ref=f'gid-cap{i:04d}')
            for i in range(250)
        ])
        resp = self.client.get(f'/audit/findings/{self.f.pk}/')
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        body = resp.json()
        # R1 is withdrawn: the detail is the ENVELOPE.
        for key in ('finding', 'comments', 'attachments', 'links',
                    'link_count', 'links_truncated'):
            self.assertIn(key, body, f'detail envelope missing {key!r}')
        self.assertEqual(len(body['links']), 200,
                         'the detail payload embeds exactly the 200-link page')
        self.assertEqual(body['link_count'], 250)
        self.assertIs(body['links_truncated'], True)

        # ... while /links/ still returns them ALL.
        full = self.client.get(links_url(self.f.pk)).json()
        self.assertEqual(full['count'], 250)
        self.assertEqual(len(full['links']), 250)

    def test_detail_under_the_cap_is_not_flagged_truncated(self):
        AuditFindingLink.objects.create(finding=self.f, kind='asana', ref='gid-one')
        body = self.client.get(f'/audit/findings/{self.f.pk}/').json()
        self.assertEqual(body['link_count'], 1)
        self.assertIs(body['links_truncated'], False)


# =========================================================================== #
# 8. Attachments — hardened allowlist, traversal, dedup, delete, note
#    (the sibling suite already pins upload shape, view_url serving, and the
#    signature 403 matrix — not repeated here)
# =========================================================================== #
@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class AttachmentHardeningTests(AuthedTestBase):
    PDF = b'%PDF-1.4\n% audit evidence body 0123456789\n' * 8

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.f = mk_finding(FY_A, 1, 'attachment hardening finding')

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def upload(self, name='evidence.pdf', content=None, content_type='application/pdf',
               note=None, finding=None):
        body = {'file': SimpleUploadedFile(name, self.PDF if content is None else content,
                                           content_type=content_type)}
        if note is not None:
            body['note'] = note
        return self.client.post(attachments_url((finding or self.f).pk), body,
                                format='multipart')

    def assert_rejected(self, resp, fragment=''):
        self.assertEqual(resp.status_code, 400, resp.content[:500])
        if fragment:
            self.assertIn(fragment, resp.json()['detail'])
        self.assertEqual(AuditFindingAttachment.objects.count(), 0,
                         'a rejected upload must not create a row')

    def test_pdf_extension_with_png_content_type_is_400(self):
        self.assert_rejected(self.upload(name='evil.pdf', content_type='image/png'))

    def test_exe_is_400_naming_what_is_allowed(self):
        resp = self.upload(name='payload.exe', content=b'MZ\x90\x00' * 10,
                           content_type='application/octet-stream')
        self.assert_rejected(resp, 'pdf')

    def test_pdf_as_octet_stream_is_400(self):
        # Extension AND content type must AGREE; a generic content type is not
        # an agreement.
        self.assert_rejected(self.upload(name='statement.pdf',
                                         content_type='application/octet-stream'))

    def test_zero_byte_file_is_400(self):
        resp = self.upload(name='empty.pdf', content=b'')
        self.assertEqual(resp.status_code, 400, resp.content[:500])
        self.assertEqual(AuditFindingAttachment.objects.count(), 0)

    def test_25mb_boundary_over_is_400(self):
        over = b'x' * (25 * 1024 * 1024 + 1)
        resp = self.upload(name='huge.pdf', content=over)
        self.assertEqual(resp.status_code, 400, f'{resp.status_code}: 25MB+1 must be 400')
        self.assertIn('25', resp.json()['detail'])

    def test_traversal_filenames_land_inside_the_finding_directory(self):
        # Django's UploadedFile basenames '../' client names before the view
        # ever sees them, so the live probe is the backslash convention a
        # Windows client sends — posixpath.basename ignores it.
        for hostile in (r'..\..\..\etc\passwd.pdf', '../../../etc/passwd.pdf'):
            resp = self.upload(name=hostile)
            self.assertEqual(resp.status_code, 201, resp.content[:300])
            att = AuditFindingAttachment.objects.get(pk=resp.json()['id'])
            expected_prefix = f'audit_findings/{FY_A}/{self.f.pk}/'
            self.assertTrue(
                att.file.name.startswith(expected_prefix),
                f'{hostile!r} stored at {att.file.name!r}, outside {expected_prefix!r}')
            leaf = att.file.name[len(expected_prefix):]
            self.assertNotIn('/', leaf)
            self.assertNotIn('\\', leaf)
            real = os.path.realpath(att.file.path)
            jail = os.path.realpath(
                os.path.join(TEMP_MEDIA, 'audit_findings', str(FY_A), str(self.f.pk)))
            self.assertTrue(real.startswith(jail + os.sep),
                            f'{hostile!r} escaped to {real!r}')
            self.assertTrue(os.path.exists(real))
        self.assertFalse(os.path.exists(os.path.join(TEMP_MEDIA, 'etc')),
                         'a traversal name created a directory outside the jail')

    def test_second_upload_of_the_same_name_never_overwrites_the_first(self):
        first_bytes = b'%PDF-1.4 first version'
        second_bytes = b'%PDF-1.4 second, different version'
        a1 = self.upload(name='aurras-statement.pdf', content=first_bytes).json()
        a2 = self.upload(name='aurras-statement.pdf', content=second_bytes).json()
        att1 = AuditFindingAttachment.objects.get(pk=a1['id'])
        att2 = AuditFindingAttachment.objects.get(pk=a2['id'])
        self.assertNotEqual(att1.file.name, att2.file.name,
                            'same client filename must get distinct disk names')
        with open(att1.file.path, 'rb') as fh:
            self.assertEqual(fh.read(), first_bytes,
                             'the FIRST file was overwritten by the second upload')
        with open(att2.file.path, 'rb') as fh:
            self.assertEqual(fh.read(), second_bytes)

    def test_delete_removes_the_row_and_the_file_from_disk(self):
        att_id = self.upload(name='to-delete.pdf').json()['id']
        path = AuditFindingAttachment.objects.get(pk=att_id).file.path
        self.assertTrue(os.path.exists(path))
        resp = self.client.delete(attachment_delete_url(att_id))
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertFalse(AuditFindingAttachment.objects.filter(pk=att_id).exists())
        self.assertFalse(os.path.exists(path), 'DELETE must remove the bytes from disk')
        self.assertEqual(self.client.delete(attachment_delete_url(att_id)).status_code,
                         404, 'a second DELETE finds nothing')

    def test_note_round_trips_on_upload_and_list(self):
        note = 'Aurras statement 31 Oct — shows R0 due'
        body = self.upload(name='aurras-oct.pdf', note=note).json()
        self.assertEqual(body.get('note'), note,
                         'CONTRACT-2 §1: the attachment dict gains note')
        listed = self.client.get(attachments_url(self.f.pk)).json()
        self.assertEqual(listed['count'], 1)
        self.assertEqual(listed['attachments'][0]['note'], note)
        self.assertIn('view_url', listed['attachments'][0])

    def test_storage_path_is_fy_then_finding_id(self):
        resp = self.upload(name='where-am-i.pdf')
        att = AuditFindingAttachment.objects.get(pk=resp.json()['id'])
        self.assertTrue(
            att.file.name.startswith(f'audit_findings/{FY_A}/{self.f.pk}/'),
            f'CONTRACT-2 §1 storage path violated: {att.file.name!r}')

    def test_note_with_nul_is_400(self):
        resp = self.upload(name='ok.pdf', note='fine\x00until here')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(AuditFindingAttachment.objects.count(), 0)


# =========================================================================== #
# 9. Graph — the reverse walk is the headline
# =========================================================================== #
class GraphTests(AuthedTestBase):
    SLIP_SHARED = sha(31)   # cited by TWO findings — the reverse-walk case
    SLIP_CROSS_FY = sha(32)  # cited from two different FYs
    SLIP_DANGLING = sha(33)  # linked but purged from the register

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        create_slips_table()
        insert_slip(cls.SLIP_SHARED, slip_ts=dt.datetime(FY_A, 3, 11, 10, 0, tzinfo=UTC),
                    ocr={'supplier': 'Builders Warehouse', 'total': '1204.50'})

        cls.fa = mk_finding(FY_A, 1, 'shared-slip finding A', check_code='BNK-05')
        cls.fb = mk_finding(FY_A, 2, 'shared-slip finding B')
        cls.f_old = mk_finding(FY_OLD, 1, 'prior-year finding citing the cross-FY slip')
        cls.f_cur = mk_finding(FY_A, 3, 'current-register finding citing the cross-FY slip')

        AuditFindingLink.objects.bulk_create([
            AuditFindingLink(finding=cls.fa, kind='slip', ref=cls.SLIP_SHARED),
            AuditFindingLink(finding=cls.fb, kind='slip', ref=cls.SLIP_SHARED),
            AuditFindingLink(finding=cls.fa, kind='asana', ref='gid-777'),
            AuditFindingLink(finding=cls.fb, kind='bank_transaction', ref='424242'),
            AuditFindingLink(finding=cls.f_old, kind='slip', ref=cls.SLIP_CROSS_FY),
            AuditFindingLink(finding=cls.f_cur, kind='slip', ref=cls.SLIP_CROSS_FY),
            AuditFindingLink(finding=cls.fb, kind='slip', ref=cls.SLIP_DANGLING),
        ])

    def graph(self, **params):
        resp = self.client.get(GRAPH_URL, params)
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        return resp.json()

    def node_ids(self, body, node_type):
        return {n['id'] for n in body['nodes'] if n['type'] == node_type}

    def test_reverse_walk_from_a_slip_finds_BOTH_citing_findings(self):
        body = self.graph(node_type='slip', node_id=self.SLIP_SHARED, depth=1)
        findings = self.node_ids(body, 'finding')
        self.assertIn(str(self.fa.pk), findings)
        self.assertIn(str(self.fb.pk), findings)
        # Edges are reported in their CANONICAL stored orientation even though
        # the walk arrived slip-first.
        keys = {edge_key(e) for e in body['edges']}
        self.assertIn(('finding', str(self.fa.pk), 'slip', 'slip', self.SLIP_SHARED), keys)
        self.assertIn(('finding', str(self.fb.pk), 'slip', 'slip', self.SLIP_SHARED), keys)

    def test_depth_2_reaches_the_findings_other_entities_and_never_bounces_back(self):
        body = self.graph(node_type='slip', node_id=self.SLIP_SHARED, depth=2)
        self.assertEqual(body['depth'], 2)
        expected = {
            ('finding', str(self.fa.pk), 'slip', 'slip', self.SLIP_SHARED),
            ('finding', str(self.fb.pk), 'slip', 'slip', self.SLIP_SHARED),
            ('finding', str(self.fa.pk), 'asana', 'asana', 'gid-777'),
            ('finding', str(self.fa.pk), 'check', 'check', 'BNK-05'),
            ('finding', str(self.fb.pk), 'bank_transaction', 'bank_transaction', '424242'),
            ('finding', str(self.fb.pk), 'slip', 'slip', self.SLIP_DANGLING),
        }
        got = [edge_key(e) for e in body['edges']]
        self.assertEqual(len(got), len(set(got)), 'the cycle guard leaked duplicate edges')
        self.assertEqual(set(got), expected,
                         'depth-2 must reach exactly the other entities of the citing '
                         'findings — nothing extra, nothing bounced back')

    def test_depth_clamps_never_400(self):
        self.assertEqual(
            self.graph(node_type='slip', node_id=self.SLIP_SHARED, depth=99)['depth'], 2,
            'depth=99 clamps to the hard cap of 2')
        self.assertEqual(
            self.graph(node_type='slip', node_id=self.SLIP_SHARED, depth=0)['depth'], 1)
        self.assertEqual(
            self.graph(node_type='slip', node_id=self.SLIP_SHARED,
                       depth='banana')['depth'], 1)

    def test_unknown_node_type_is_400_naming_the_valid_types(self):
        resp = self.client.get(GRAPH_URL, {'node_type': 'carrier_pigeon', 'node_id': 'x'})
        self.assertEqual(resp.status_code, 400, resp.content[:300])
        detail = resp.json()['detail']
        for t in ('finding', 'slip', 'attachment', 'check'):
            self.assertIn(t, detail)

    def test_half_a_seed_is_400_both_ways(self):
        self.assertEqual(
            self.client.get(GRAPH_URL, {'node_type': 'slip'}).status_code, 400)
        self.assertEqual(
            self.client.get(GRAPH_URL, {'node_id': self.SLIP_SHARED}).status_code, 400)

    def test_garbage_fy_is_400(self):
        self.assertEqual(self.client.get(GRAPH_URL, {'fy': 'banana'}).status_code, 400)

    def test_node_seeded_walk_without_fy_spans_ALL_fys(self):
        # SENIOR RULING: a caller naming a node asks "which findings cite THIS",
        # not "which findings in some default year". The FY_A finding must be
        # found even though the CURRENT fy (CUR) is a later, empty year — and
        # the prior-year citation must surface right beside it.
        self.assertGreater(CUR, FY_A, 'test-env sanity: the current FY is later than FY_A')
        body = self.graph(node_type='slip', node_id=self.SLIP_CROSS_FY, depth=1)
        self.assertEqual(body['current_fy'], CUR)
        findings = self.node_ids(body, 'finding')
        self.assertIn(str(self.f_cur.pk), findings,
                      'a node-seeded walk must find the FY_A finding despite the '
                      'current FY having moved on')
        self.assertIn(str(self.f_old.pk), findings,
                      'a node-seeded walk with no fy spans ALL FYs')

    def test_explicit_fy_still_overrides_on_a_node_seeded_walk(self):
        body = self.graph(node_type='slip', node_id=self.SLIP_CROSS_FY, depth=1,
                          fy=str(FY_A))
        findings = self.node_ids(body, 'finding')
        self.assertIn(str(self.f_cur.pk), findings)
        self.assertNotIn(str(self.f_old.pk), findings)

    def test_no_node_applies_the_fy_default_resolve_default_fy(self):
        # With findings in FY_A and FY_OLD, the read default is the LATEST FY
        # with findings (FY_A) — same rule as the list endpoint.
        body = self.graph()
        self.assertEqual(body['fy'], FY_A)
        findings = self.node_ids(body, 'finding')
        self.assertIn(str(self.fa.pk), findings)
        self.assertIn(str(self.fb.pk), findings)
        self.assertNotIn(str(self.f_old.pk), findings,
                         'the no-node graph must respect the resolved FY filter')

    def test_no_node_fy_all_spans_everything(self):
        body = self.graph(fy='all')
        self.assertIn(str(self.f_old.pk), self.node_ids(body, 'finding'))

    def test_labels_finding_slip_check_and_dangling(self):
        body = self.graph(node_type='slip', node_id=self.SLIP_SHARED, depth=2)
        by_node = {(n['type'], n['id']): n for n in body['nodes']}

        fa_node = by_node[('finding', str(self.fa.pk))]
        self.assertIn(self.fa.ref, fa_node['label'])
        self.assertIn('shared-slip finding A', fa_node['label'])

        slip_node = by_node[('slip', self.SLIP_SHARED)]
        self.assertIn('Builders Warehouse', slip_node['label'])
        self.assertTrue(slip_node['url'], 'a resolvable slip node carries its signed url')

        self.assertEqual(by_node[('check', 'BNK-05')]['label'], 'BNK-05')

        dangling = by_node[('slip', self.SLIP_DANGLING)]
        self.assertEqual(dangling['label'], self.SLIP_DANGLING,
                         'an unresolvable node still appears, labelled with its raw id')

    def test_label_resolution_is_O_node_types(self):
        f_small = mk_finding(FY_A, 4, 'graph batching probe — small')
        f_large = mk_finding(FY_A, 5, 'graph batching probe — large')

        def seed(finding, n):
            rows = []
            for i in range(n):
                rows += [
                    AuditFindingLink(finding=finding, kind='slip',
                                     ref=sha(900 + finding.pk * 50 + i)),
                    AuditFindingLink(finding=finding, kind='bank_transaction',
                                     ref=str(550000 + finding.pk * 50 + i)),
                    AuditFindingLink(finding=finding, kind='asana',
                                     ref=f'gid-g{finding.pk}-{i}'),
                ]
            AuditFindingLink.objects.bulk_create(rows)

        seed(f_small, 2)
        seed(f_large, 12)

        with CaptureQueriesContext(connection) as ctx_small:
            body = self.graph(node_type='finding', node_id=str(f_small.pk), depth=1)
            self.assertEqual(len(body['edges']), 6)
        with CaptureQueriesContext(connection) as ctx_large:
            body = self.graph(node_type='finding', node_id=str(f_large.pk), depth=1)
            self.assertEqual(len(body['edges']), 36)

        self.assertEqual(
            len(ctx_small), len(ctx_large),
            f'graph query count grew with node count ({len(ctx_small)} -> '
            f'{len(ctx_large)}): label resolution must be one query per node TYPE')
        self.assertLessEqual(len(ctx_large), 12,
                             f'{len(ctx_large)} queries for one depth-1 walk is not '
                             'O(node types)')


class GraphTruncationTests(AuthedTestBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.f = mk_finding(FY_A, 1, 'truncation probe finding')
        AuditFindingLink.objects.bulk_create([
            AuditFindingLink(finding=cls.f, kind='asana', ref=f'gid-tr{i:04d}')
            for i in range(501)
        ])

    def test_no_node_branch_caps_at_500_edges_and_flags_truncated(self):
        resp = self.client.get(GRAPH_URL, {'fy': str(FY_A)})
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        body = resp.json()
        self.assertIs(body['truncated'], True)
        self.assertEqual(len(body['edges']), 500,
                         'the cap is exactly 500 edges, one page of graph')

    def test_node_seeded_walk_caps_at_500_edges_too(self):
        resp = self.client.get(GRAPH_URL, {'node_type': 'finding',
                                           'node_id': str(self.f.pk), 'depth': '1'})
        body = resp.json()
        self.assertIs(body['truncated'], True)
        self.assertEqual(len(body['edges']), 500)
