"""
Adversarial tests for apps.pricelist — written against the docstring contract in
models.py / services.py / views.py, NOT against the implementation.

Conventions:
* Django ``TestCase`` (per-test rollback). Every DB-constraint violation is
  asserted INSIDE ``with transaction.atomic():`` so the IntegrityError is
  confined to a savepoint and the rest of the method keeps working.
* Fixtures are explicit and local (no import from seed_data) so a rate-card
  change cannot silently move an expected value. The single seed test only
  asserts idempotency (counts equal across two runs), never specific prices.
* Money is asserted as 2-dp STRINGS / Decimals, never floats.
* ``raise_request_exception = False`` on the API client so an unhandled server
  exception shows up as a 500 status (a clean assertion failure) rather than a
  traceback that hides which endpoint fell over.
"""
import csv
import datetime as dt
import io
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_metadata.models import XeroContacts

from .models import PriceListItem, PriceListPrice
from .services import add_price, build_quote, get_price, resolve_price

D = dt.date  # shorthand
ONE_DAY = dt.timedelta(days=1)


# --------------------------------------------------------------------------- #
# Fixture helpers (explicit, local — NOT the seed rate card)
# --------------------------------------------------------------------------- #
_contact_seq = {'n': 0}


def _make_tenant():
    tenant, _ = XeroTenant.objects.get_or_create(tenant_id='test-tenant', defaults={'tenant_name': 'Test Tenant'})
    return tenant


def _make_contact(name, contacts_id=None):
    """XeroContacts needs a NOT NULL organisation (XeroTenant); PK is the contacts_id string.

    Created with ``bulk_create`` **on purpose**: ``XeroContacts.save()`` fires the
    ``request_glossary_refresh_on_contact`` post_save receiver
    (apps/xero/xero_metadata/signals.py), which writes ``XeroTenant.tenant_id`` — a
    varchar UUID — into ``ai_agent.GlossaryRefreshRequest.organisation_id``, which is an
    ``IntegerField``. That raises ``ValueError: Field 'organisation_id' expected a number``.
    It is a PRE-EXISTING bug in another app, unrelated to the price list; production never
    trips it because ``XeroContactsModelManager.create_contacts_from_xero`` also uses
    ``bulk_create`` (which does not send signals). Reported to MC separately — do NOT
    "fix" it here by changing another app's model.
    """
    _contact_seq['n'] += 1
    cid = contacts_id or f'00000000-0000-0000-0000-{_contact_seq["n"]:012d}'
    XeroContacts.objects.bulk_create(
        [XeroContacts(organisation=_make_tenant(), contacts_id=cid, name=name)]
    )
    return XeroContacts.objects.get(pk=cid)


def _item(code='DB-V10P', name='d&b V10P top', category='PA', unit='DAY', **kw):
    return PriceListItem.objects.create(code=code, name=name, category=category, unit=unit, **kw)


def _price(item, price, valid_from, valid_to=None, price_type='LIST', customer=None, **kw):
    """Direct ORM insert — bypasses add_price so tests can build exact histories."""
    return PriceListPrice.objects.create(
        item=item, price=Decimal(str(price)), valid_from=valid_from, valid_to=valid_to,
        price_type=price_type, customer=customer, **kw,
    )


# =========================================================================== #
# 1. get_price / resolve_price — effective dating
# =========================================================================== #
class EffectiveDatingTests(TestCase):
    def setUp(self):
        self.item = _item()
        # closed: 2025-01-01 .. 2025-06-30 @ 700
        # closed: 2025-07-01 .. 2025-12-31 @ 800
        # open:   2026-01-01 .. NULL       @ 850
        self.r1 = _price(self.item, '700.00', D(2025, 1, 1), D(2025, 6, 30))
        self.r2 = _price(self.item, '800.00', D(2025, 7, 1), D(2025, 12, 31))
        self.r3 = _price(self.item, '850.00', D(2026, 1, 1), None)

    def test_price_valid_exactly_on_valid_from(self):
        """Off-by-one: a price must apply on its own valid_from day."""
        self.assertEqual(get_price(self.item, on_date=D(2025, 7, 1)).id, self.r2.id)
        self.assertEqual(get_price(self.item, on_date=D(2026, 1, 1)).id, self.r3.id)

    def test_price_valid_exactly_on_valid_to(self):
        """Off-by-one: valid_to is INCLUSIVE — the last day still resolves to that row."""
        self.assertEqual(get_price(self.item, on_date=D(2025, 6, 30)).id, self.r1.id)
        self.assertEqual(get_price(self.item, on_date=D(2025, 12, 31)).id, self.r2.id)

    def test_day_before_valid_from_resolves_to_previous_row(self):
        """A new price must not leak backwards onto the day before it starts."""
        self.assertEqual(get_price(self.item, on_date=D(2025, 12, 31)).id, self.r2.id)
        self.assertEqual(get_price(self.item, on_date=D(2025, 6, 30)).id, self.r1.id)

    def test_day_before_first_row_is_none(self):
        """No price existed before the first row — must be None, not the first row."""
        self.assertIsNone(get_price(self.item, on_date=D(2024, 12, 31)))
        out = resolve_price(self.item, on_date=D(2024, 12, 31))
        self.assertFalse(out['resolved'])
        self.assertIsNone(out['price'])

    def test_day_after_valid_to_resolves_to_next_row(self):
        """A closed price must not bleed into the day after its valid_to."""
        self.assertEqual(get_price(self.item, on_date=D(2025, 7, 1)).id, self.r2.id)
        self.assertEqual(get_price(self.item, on_date=D(2026, 1, 1)).id, self.r3.id)

    def test_open_row_resolves_far_in_future(self):
        """valid_to=NULL means 'still current' — must resolve decades ahead."""
        row = get_price(self.item, on_date=D(2099, 12, 31))
        self.assertEqual(row.id, self.r3.id)
        self.assertEqual(resolve_price(self.item, on_date=D(2099, 12, 31))['price'], '850.00')

    def test_each_date_lands_on_the_right_row(self):
        """Two closed + one open row: a mid-range date lands on exactly its row."""
        self.assertEqual(resolve_price(self.item, on_date=D(2025, 3, 15))['price'], '700.00')
        self.assertEqual(resolve_price(self.item, on_date=D(2025, 9, 15))['price'], '800.00')
        self.assertEqual(resolve_price(self.item, on_date=D(2026, 6, 15))['price'], '850.00')

    def test_no_rows_returns_none_and_resolved_false(self):
        """Item with no prices: None / resolved=False, not an exception."""
        bare = _item(code='BARE')
        self.assertIsNone(get_price(bare, on_date=D(2026, 1, 1)))
        out = resolve_price(bare, on_date=D(2026, 1, 1))
        self.assertFalse(out['resolved'])
        self.assertIsNone(out['price'])
        self.assertIsNone(out['price_id'])
        self.assertFalse(out['fallback_to_list'])

    def test_on_date_defaults_to_today(self):
        """on_date=None must use today's (local) date, inclusive of a row starting today."""
        today = timezone.localdate()
        fresh = _item(code='TODAY')
        _price(fresh, '10.00', today - ONE_DAY, today - ONE_DAY)  # yesterday only
        row_today = _price(fresh, '20.00', today, None)
        self.assertEqual(get_price(fresh).id, row_today.id)
        out = resolve_price(fresh)
        self.assertEqual(out['date'], today.isoformat())
        self.assertEqual(out['price'], '20.00')

    def test_resolve_price_reports_the_row_metadata(self):
        """resolve_price must echo the row's valid_from/valid_to/price_id/price_type faithfully."""
        out = resolve_price(self.item, on_date=D(2025, 8, 1))
        self.assertEqual(out['price_id'], self.r2.id)
        self.assertEqual(out['valid_from'], '2025-07-01')
        self.assertEqual(out['valid_to'], '2025-12-31')
        self.assertEqual(out['price_type'], 'LIST')
        self.assertEqual(out['requested_price_type'], 'LIST')
        self.assertFalse(out['fallback_to_list'])


# =========================================================================== #
# 2. get_price — customer override
# =========================================================================== #
class CustomerOverrideTests(TestCase):
    def setUp(self):
        self.item = _item()
        self.aurras = _make_contact('AURRAS GROUP (PTY) LTD')
        self.other = _make_contact('SOME OTHER CUSTOMER')
        self.list_row = _price(self.item, '850.00', D(2025, 12, 3), None)
        self.trade_row = _price(self.item, '680.00', D(2025, 12, 3), None, price_type='TRADE', customer=self.aurras)

    def test_customer_trade_row_beats_general_list_row(self):
        """Customer-specific lane must win over the general LIST row on the same date."""
        row = get_price(self.item, on_date=D(2026, 1, 10), customer=self.aurras)
        self.assertEqual(row.id, self.trade_row.id)
        out = resolve_price(self.item, on_date=D(2026, 1, 10), customer=self.aurras)
        self.assertEqual(out['price'], '680.00')
        self.assertEqual(out['price_type'], 'TRADE')
        self.assertEqual(out['customer_id'], self.aurras.contacts_id)
        self.assertEqual(out['customer_name'], 'AURRAS GROUP (PTY) LTD')
        self.assertFalse(out['fallback_to_list'])

    def test_customer_row_does_not_leak_to_other_customer(self):
        """One customer's trade rate must NEVER be returned for a different customer."""
        row = get_price(self.item, on_date=D(2026, 1, 10), customer=self.other)
        self.assertEqual(row.id, self.list_row.id)
        out = resolve_price(self.item, on_date=D(2026, 1, 10), customer=self.other)
        self.assertEqual(out['price'], '850.00')
        self.assertEqual(out['price_type'], 'LIST')
        # the requested customer is echoed, not Aurras
        self.assertEqual(out['customer_id'], self.other.contacts_id)
        self.assertNotEqual(out['customer_name'], 'AURRAS GROUP (PTY) LTD')

    def test_no_customer_never_returns_a_customer_row(self):
        """Anonymous lookup must get the general LIST row, not some customer's override."""
        row = get_price(self.item, on_date=D(2026, 1, 10))
        self.assertEqual(row.id, self.list_row.id)

    def test_expired_customer_row_falls_back_to_list(self):
        """Once the customer deal expires, the general LIST price must apply again."""
        self.trade_row.valid_to = D(2025, 12, 31)
        self.trade_row.save()
        self.assertEqual(get_price(self.item, on_date=D(2025, 12, 31), customer=self.aurras).id, self.trade_row.id)
        self.assertEqual(get_price(self.item, on_date=D(2026, 1, 1), customer=self.aurras).id, self.list_row.id)
        self.assertEqual(resolve_price(self.item, on_date=D(2026, 1, 1), customer=self.aurras)['price'], '850.00')

    def test_trade_without_customer_falls_back_to_list_with_flag(self):
        """Asking TRADE with no TRADE lane → LIST row AND fallback_to_list=True (caller must know)."""
        row = get_price(self.item, on_date=D(2026, 1, 10), price_type='TRADE')
        self.assertEqual(row.id, self.list_row.id)
        out = resolve_price(self.item, on_date=D(2026, 1, 10), price_type='TRADE')
        self.assertEqual(out['requested_price_type'], 'TRADE')
        self.assertEqual(out['price_type'], 'LIST')
        self.assertTrue(out['fallback_to_list'])

    def test_general_trade_row_is_preferred_over_list_when_asked(self):
        """A general (customer=NULL) TRADE row must be returned for price_type='TRADE', no fallback flag."""
        gen_trade = _price(self.item, '700.00', D(2025, 12, 3), None, price_type='TRADE')
        out = resolve_price(self.item, on_date=D(2026, 1, 10), price_type='TRADE')
        self.assertEqual(out['price_id'], gen_trade.id)
        self.assertEqual(out['price'], '700.00')
        self.assertFalse(out['fallback_to_list'])

    def test_list_request_with_only_list_row_is_not_flagged_fallback(self):
        """fallback_to_list must be False when LIST was requested and LIST was found."""
        out = resolve_price(self.item, on_date=D(2026, 1, 10), price_type='LIST')
        self.assertTrue(out['resolved'])
        self.assertFalse(out['fallback_to_list'])

    def test_customer_as_contacts_id_string_behaves_like_instance(self):
        """Passing the contacts_id string must resolve exactly like passing the instance."""
        by_inst = resolve_price(self.item, on_date=D(2026, 1, 10), customer=self.aurras)
        by_str = resolve_price(self.item, on_date=D(2026, 1, 10), customer=self.aurras.contacts_id)
        self.assertEqual(by_str['price_id'], by_inst['price_id'])
        self.assertEqual(by_str['price'], '680.00')
        self.assertEqual(by_str['customer_id'], self.aurras.contacts_id)
        # the name is recoverable from the row when only the id string was given
        self.assertEqual(by_str['customer_name'], 'AURRAS GROUP (PTY) LTD')

    def test_unknown_customer_id_string_falls_back_to_list(self):
        """A contacts_id that has no rows must fall through to the general LIST row, not None."""
        out = resolve_price(self.item, on_date=D(2026, 1, 10), customer='no-such-contact')
        self.assertTrue(out['resolved'])
        self.assertEqual(out['price'], '850.00')

    def test_price_type_is_case_insensitive(self):
        """'trade' must be treated as 'TRADE'."""
        out = resolve_price(self.item, on_date=D(2026, 1, 10), customer=self.aurras, price_type='trade')
        self.assertEqual(out['requested_price_type'], 'TRADE')
        self.assertEqual(out['price'], '680.00')


# =========================================================================== #
# 3. The exclusion constraint (real DB constraint)
# =========================================================================== #
class OverlapConstraintTests(TestCase):
    def setUp(self):
        self.item = _item()
        self.other_item = _item(code='DB-D80', name='d&b D80 amp', category='AMP')
        self.c1 = _make_contact('Customer One')
        self.c2 = _make_contact('Customer Two')

    def test_second_open_list_row_same_item_rejected(self):
        """Two open general LIST rows for one item would make 'current price' ambiguous."""
        _price(self.item, '850.00', D(2025, 12, 3), None)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _price(self.item, '900.00', D(2026, 1, 1), None)
        self.assertEqual(PriceListPrice.objects.filter(item=self.item).count(), 1)

    def test_open_row_rejected_when_dated_before_existing_open_row(self):
        """An open row inserted BEFORE the existing open row also overlaps (NULL is unbounded above)."""
        _price(self.item, '850.00', D(2025, 12, 3), None)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _price(self.item, '800.00', D(2025, 1, 1), None)

    def test_list_row_overlapping_closed_list_row_rejected(self):
        """A LIST row that overlaps a CLOSED LIST row must be rejected too (not only open rows)."""
        _price(self.item, '700.00', D(2025, 1, 1), D(2025, 6, 30))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _price(self.item, '750.00', D(2025, 6, 30), D(2025, 12, 31))  # shares 2025-06-30
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _price(self.item, '750.00', D(2025, 3, 1), D(2025, 3, 31))  # fully inside
        self.assertEqual(PriceListPrice.objects.filter(item=self.item).count(), 1)

    def test_non_overlapping_adjacent_list_row_accepted(self):
        """A row starting the day AFTER the previous valid_to must be accepted (inclusive bounds)."""
        _price(self.item, '700.00', D(2025, 1, 1), D(2025, 6, 30))
        row = _price(self.item, '750.00', D(2025, 7, 1), None)
        self.assertIsNotNone(row.pk)
        self.assertEqual(PriceListPrice.objects.filter(item=self.item).count(), 2)

    def test_open_rows_for_different_customers_both_accepted(self):
        """Constraint is partial (customer IS NULL only): two customers may each have an open row."""
        a = _price(self.item, '680.00', D(2025, 12, 3), None, price_type='LIST', customer=self.c1)
        b = _price(self.item, '690.00', D(2025, 12, 3), None, price_type='LIST', customer=self.c2)
        self.assertIsNotNone(a.pk)
        self.assertIsNotNone(b.pk)

    def test_same_customer_two_open_rows_accepted_and_newest_wins(self):
        """Customer lanes are NOT constrained — two open rows are allowed and get_price picks the newest."""
        _price(self.item, '680.00', D(2025, 12, 3), None, price_type='TRADE', customer=self.c1)
        newer = _price(self.item, '650.00', D(2026, 1, 1), None, price_type='TRADE', customer=self.c1)
        self.assertEqual(get_price(self.item, on_date=D(2026, 2, 1), customer=self.c1).id, newer.id)

    def test_open_list_rows_for_different_items_both_accepted(self):
        """Constraint is per item — other items must be unaffected."""
        _price(self.item, '850.00', D(2025, 12, 3), None)
        row = _price(self.other_item, '1500.00', D(2025, 12, 3), None)
        self.assertIsNotNone(row.pk)

    def test_trade_row_overlapping_list_row_accepted(self):
        """TRADE / SPECIAL lanes are overrides — they may overlap the LIST lane freely."""
        _price(self.item, '850.00', D(2025, 12, 3), None)
        t = _price(self.item, '700.00', D(2025, 12, 3), None, price_type='TRADE')
        s = _price(self.item, '600.00', D(2025, 12, 3), None, price_type='SPECIAL')
        self.assertIsNotNone(t.pk)
        self.assertIsNotNone(s.pk)

    def test_customer_list_row_overlapping_general_list_row_accepted(self):
        """A customer-specific row labelled LIST is still a customer lane — not covered by the constraint."""
        _price(self.item, '850.00', D(2025, 12, 3), None)
        row = _price(self.item, '800.00', D(2025, 12, 3), None, price_type='LIST', customer=self.c1)
        self.assertIsNotNone(row.pk)


# =========================================================================== #
# 4. add_price
# =========================================================================== #
class AddPriceTests(TestCase):
    def setUp(self):
        self.item = _item()
        self.cust = _make_contact('Trade Customer')

    def test_first_price_in_lane_is_open_and_nothing_closed(self):
        """First row in a lane: created open, closed=None."""
        row, closed = add_price(self.item, price='850', valid_from=D(2025, 12, 3))
        self.assertIsNone(closed)
        self.assertIsNone(row.valid_to)
        self.assertEqual(row.price, Decimal('850.00'))
        self.assertEqual(row.price_type, 'LIST')
        self.assertIsNone(row.customer_id)

    def test_new_list_price_closes_previous_open_row_to_day_before(self):
        """Previous open row must be closed to (new valid_from - 1 day) — contiguous, no gap, no overlap."""
        first, _ = add_price(self.item, price='850', valid_from=D(2025, 12, 3))
        second, closed = add_price(self.item, price='900', valid_from=D(2026, 3, 1))
        self.assertIsNotNone(closed)
        self.assertEqual(closed.id, first.id)
        self.assertEqual(closed.valid_to, D(2026, 2, 28))
        first.refresh_from_db()
        self.assertEqual(first.valid_to, D(2026, 2, 28))
        self.assertIsNone(second.valid_to)
        # and resolution honours the boundary both sides
        self.assertEqual(get_price(self.item, on_date=D(2026, 2, 28)).id, first.id)
        self.assertEqual(get_price(self.item, on_date=D(2026, 3, 1)).id, second.id)

    def test_readding_same_valid_from_updates_in_place(self):
        """Idempotency: same valid_from must UPDATE the row (no new row, closed=None)."""
        first, _ = add_price(self.item, price='850', valid_from=D(2025, 12, 3), note='seed', set_by='mc')
        again, closed = add_price(self.item, price='860', valid_from=D(2025, 12, 3))
        self.assertIsNone(closed)
        self.assertEqual(again.id, first.id)
        self.assertEqual(PriceListPrice.objects.filter(item=self.item).count(), 1)
        first.refresh_from_db()
        self.assertEqual(first.price, Decimal('860.00'))
        self.assertIsNone(first.valid_to)
        # blank note / set_by must NOT wipe the existing ones
        self.assertEqual(first.note, 'seed')
        self.assertEqual(first.set_by, 'mc')

    def test_readding_identical_price_is_a_noop(self):
        """Re-running the seed with the same values must not create, close, or change anything."""
        add_price(self.item, price='850', valid_from=D(2025, 12, 3))
        row, closed = add_price(self.item, price='850.00', valid_from=D(2025, 12, 3))
        self.assertIsNone(closed)
        self.assertEqual(PriceListPrice.objects.filter(item=self.item).count(), 1)
        self.assertEqual(row.price, Decimal('850.00'))

    def test_back_dating_raises_and_leaves_db_unchanged(self):
        """Back-dating into a closed period must raise ValidationError and roll back EVERYTHING."""
        first, _ = add_price(self.item, price='850', valid_from=D(2025, 12, 3))
        second, _ = add_price(self.item, price='900', valid_from=D(2026, 3, 1))
        with self.assertRaises(ValidationError):
            add_price(self.item, price='875', valid_from=D(2026, 1, 15))
        self.assertEqual(PriceListPrice.objects.filter(item=self.item).count(), 2)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.valid_to, D(2026, 2, 28))
        self.assertIsNone(second.valid_to)
        self.assertEqual(second.price, Decimal('900.00'))

    def test_back_dating_before_single_open_row_raises(self):
        """Even with only one (open) row, a new row dated earlier must be refused, not inserted."""
        only, _ = add_price(self.item, price='850', valid_from=D(2025, 12, 3))
        with self.assertRaises(ValidationError):
            add_price(self.item, price='800', valid_from=D(2025, 1, 1))
        self.assertEqual(PriceListPrice.objects.filter(item=self.item).count(), 1)
        only.refresh_from_db()
        self.assertIsNone(only.valid_to)

    def test_insert_inside_hand_closed_period_is_rejected_and_leaves_db_unchanged(self):
        """Docstring: 'back-dating into a closed period is refused'. A row dated AFTER the newest
        valid_from but INSIDE its hand-closed range is still a back-date into a closed period; it
        must be rejected (ValidationError per the docstring, IntegrityError at worst) and leave
        the lane untouched."""
        closed_row = _price(self.item, '700.00', D(2025, 1, 1), D(2025, 12, 31))
        with self.assertRaises((ValidationError, IntegrityError)):
            with transaction.atomic():
                add_price(self.item, price='750', valid_from=D(2025, 6, 1))
        self.assertEqual(PriceListPrice.objects.filter(item=self.item).count(), 1)
        closed_row.refresh_from_db()
        self.assertEqual(closed_row.valid_to, D(2025, 12, 31))

    def test_insert_inside_hand_closed_trade_period_is_refused(self):
        """Same scenario in a TRADE lane, where there is NO exclusion constraint to backstop: the
        docstring promises 'back-dating into a closed period is refused with ValidationError'. If
        this silently inserts, the lane has two rows valid on the same day and resolution depends
        on row order — history has been rewritten without anyone being told."""
        closed_row = _price(self.item, '600.00', D(2025, 1, 1), D(2025, 12, 31), price_type='TRADE')
        with self.assertRaises(ValidationError):
            add_price(self.item, price='650', valid_from=D(2025, 6, 1), price_type='TRADE')
        self.assertEqual(PriceListPrice.objects.filter(item=self.item, price_type='TRADE').count(), 1)
        closed_row.refresh_from_db()
        self.assertEqual(closed_row.valid_to, D(2025, 12, 31))

    def test_closing_is_per_lane_trade_does_not_close_list(self):
        """Adding a TRADE price must NOT close the open LIST row (different lane)."""
        list_row, _ = add_price(self.item, price='850', valid_from=D(2025, 12, 3))
        trade_row, closed = add_price(self.item, price='680', valid_from=D(2026, 1, 1), price_type='TRADE')
        self.assertIsNone(closed)
        list_row.refresh_from_db()
        self.assertIsNone(list_row.valid_to)
        self.assertIsNone(trade_row.valid_to)
        self.assertEqual(trade_row.price_type, 'TRADE')

    def test_closing_is_per_lane_customer_does_not_close_general(self):
        """Adding a customer-specific row must NOT close the general row, and vice versa."""
        general, _ = add_price(self.item, price='850', valid_from=D(2025, 12, 3))
        cust_row, closed = add_price(self.item, price='680', valid_from=D(2026, 1, 1), customer=self.cust)
        self.assertIsNone(closed)
        general.refresh_from_db()
        self.assertIsNone(general.valid_to)
        self.assertEqual(cust_row.customer_id, self.cust.contacts_id)
        # the reverse: a new general price must not close the customer row
        _, closed2 = add_price(self.item, price='900', valid_from=D(2026, 6, 1))
        self.assertEqual(closed2.id, general.id)
        cust_row.refresh_from_db()
        self.assertIsNone(cust_row.valid_to)

    def test_customer_lane_closes_only_that_customers_open_row(self):
        """A second price for customer A must close A's previous row, not customer B's."""
        other = _make_contact('Other Customer')
        a1, _ = add_price(self.item, price='680', valid_from=D(2025, 12, 3), price_type='TRADE', customer=self.cust)
        b1, _ = add_price(self.item, price='690', valid_from=D(2025, 12, 3), price_type='TRADE', customer=other)
        a2, closed = add_price(self.item, price='650', valid_from=D(2026, 2, 1), price_type='TRADE', customer=self.cust)
        self.assertEqual(closed.id, a1.id)
        a1.refresh_from_db()
        b1.refresh_from_db()
        self.assertEqual(a1.valid_to, D(2026, 1, 31))
        self.assertIsNone(b1.valid_to)

    def test_customer_as_id_string_in_add_price(self):
        """add_price must accept the contacts_id string and attach the FK correctly."""
        row, _ = add_price(self.item, price='680', valid_from=D(2025, 12, 3), customer=self.cust.contacts_id)
        self.assertEqual(row.customer_id, self.cust.contacts_id)
        self.assertEqual(get_price(self.item, on_date=D(2026, 1, 1), customer=self.cust).id, row.id)

    def test_negative_price_rejected(self):
        """Negative prices must raise ValidationError and write nothing."""
        with self.assertRaises(ValidationError):
            add_price(self.item, price='-1', valid_from=D(2025, 12, 3))
        self.assertEqual(PriceListPrice.objects.count(), 0)

    def test_zero_price_accepted(self):
        """Zero is a legitimate price (free item / included) — must NOT be rejected."""
        row, _ = add_price(self.item, price='0', valid_from=D(2025, 12, 3))
        self.assertEqual(row.price, Decimal('0.00'))

    def test_garbage_price_string_rejected(self):
        """'abc' must be rejected with a clear error (ValidationError or ValueError), nothing written."""
        with self.assertRaises((ValidationError, ValueError)):
            add_price(self.item, price='abc', valid_from=D(2025, 12, 3))
        with self.assertRaises((ValidationError, ValueError)):
            add_price(self.item, price='', valid_from=D(2025, 12, 3))
        with self.assertRaises((ValidationError, ValueError)):
            add_price(self.item, price=None, valid_from=D(2025, 12, 3))
        self.assertEqual(PriceListPrice.objects.count(), 0)

    def test_nan_and_infinity_price_rejected_cleanly(self):
        """'NaN' / 'Infinity' parse as Decimal but are not money — must be rejected with
        ValidationError/ValueError, NOT leak decimal.InvalidOperation."""
        for junk in ('NaN', 'nan', 'Infinity', '-Infinity', 'sNaN'):
            with self.subTest(price=junk):
                with self.assertRaises((ValidationError, ValueError)):
                    add_price(self.item, price=junk, valid_from=D(2025, 12, 3))
        self.assertEqual(PriceListPrice.objects.count(), 0)

    def test_price_exceeding_column_precision_rejected_cleanly(self):
        """numeric(12,2) holds < 10^10; a bigger price must be a ValidationError, not a DB DataError."""
        with self.assertRaises((ValidationError, ValueError)):
            with transaction.atomic():
                add_price(self.item, price='99999999999999', valid_from=D(2025, 12, 3))
        self.assertEqual(PriceListPrice.objects.count(), 0)

    def test_bad_price_type_rejected(self):
        """Unknown price_type must raise ValidationError."""
        with self.assertRaises(ValidationError):
            add_price(self.item, price='850', valid_from=D(2025, 12, 3), price_type='WHOLESALE')
        self.assertEqual(PriceListPrice.objects.count(), 0)

    def test_price_type_lowercase_accepted(self):
        """'trade' must be normalised to 'TRADE'."""
        row, _ = add_price(self.item, price='680', valid_from=D(2025, 12, 3), price_type='trade')
        self.assertEqual(row.price_type, 'TRADE')

    def test_valid_from_iso_string_accepted(self):
        """valid_from may be an ISO string."""
        row, _ = add_price(self.item, price='850', valid_from='2025-12-03')
        self.assertEqual(row.valid_from, D(2025, 12, 3))

    def test_valid_from_garbage_rejected(self):
        """Non-date valid_from must be rejected, nothing written."""
        with self.assertRaises((ValidationError, ValueError)):
            add_price(self.item, price='850', valid_from='not-a-date')
        with self.assertRaises(ValidationError):
            add_price(self.item, price='850', valid_from=None)
        self.assertEqual(PriceListPrice.objects.count(), 0)

    def test_price_is_quantised_half_up(self):
        """A 3-dp input must be stored rounded HALF_UP to cents (850.005 → 850.01)."""
        row, _ = add_price(self.item, price='850.005', valid_from=D(2025, 12, 3))
        self.assertEqual(row.price, Decimal('850.01'))

    def test_two_sequential_adds_then_readd_middle_is_update_not_reslice(self):
        """Re-setting the (now closed) first row's valid_from updates price in place and keeps valid_to."""
        first, _ = add_price(self.item, price='850', valid_from=D(2025, 12, 3))
        add_price(self.item, price='900', valid_from=D(2026, 3, 1))
        row, closed = add_price(self.item, price='855', valid_from=D(2025, 12, 3))
        self.assertIsNone(closed)
        self.assertEqual(row.id, first.id)
        first.refresh_from_db()
        self.assertEqual(first.price, Decimal('855.00'))
        self.assertEqual(first.valid_to, D(2026, 2, 28))
        self.assertEqual(PriceListPrice.objects.filter(item=self.item).count(), 2)


# =========================================================================== #
# 5. build_quote — the maths
# =========================================================================== #
class BuildQuoteTests(TestCase):
    def setUp(self):
        self.on = D(2026, 2, 1)
        self.top = _item(code='DB-V10P', name='d&b V10P top', category='PA')
        self.amp = _item(code='DB-D40', name='d&b D40 amp', category='AMP')
        self.cent = _item(code='CENT', name='thirty cents', category='OTHER')
        _price(self.top, '850.00', D(2025, 12, 3), None)
        _price(self.amp, '1400.00', D(2025, 12, 3), None)
        _price(self.cent, '0.30', D(2025, 12, 3), None)
        self.aurras = _make_contact('AURRAS GROUP (PTY) LTD')
        _price(self.top, '680.00', D(2025, 12, 3), None, price_type='TRADE', customer=self.aurras)

    def _check_identity(self, q):
        """ex_vat + vat == incl_vat and subtotal - discount == ex_vat EXACTLY as Decimals."""
        self.assertEqual(Decimal(q['ex_vat']) + Decimal(q['vat']), Decimal(q['incl_vat']))
        self.assertEqual(Decimal(q['subtotal']) - Decimal(q['discount']), Decimal(q['ex_vat']))
        self.assertEqual(sum(Decimal(ln['line_total']) for ln in q['lines']), Decimal(q['subtotal']))

    def test_single_line_qty1_days1_equals_unit_price(self):
        """qty=1 days=1: line_total == unit_price, and the defaults fill in."""
        q = build_quote([{'code': 'DB-V10P'}], on_date=self.on)
        ln = q['lines'][0]
        self.assertTrue(ln['priced'])
        self.assertEqual(ln['unit_price'], '850.00')
        self.assertEqual(ln['line_total'], '850.00')
        self.assertEqual(ln['qty'], '1')
        self.assertEqual(ln['days'], '1')
        self.assertEqual(ln['price_type'], 'LIST')
        self.assertEqual(q['subtotal'], '850.00')
        self.assertEqual(q['discount'], '0.00')
        self.assertEqual(q['ex_vat'], '850.00')
        self.assertEqual(q['vat'], '127.50')
        self.assertEqual(q['incl_vat'], '977.50')
        self.assertEqual(q['vat_rate'], '0.15')
        self.assertEqual(q['warnings'], [])
        self._check_identity(q)

    def test_qty_times_days_multiplies(self):
        """850 x 4 x 3 = 10200.00."""
        q = build_quote([{'code': 'DB-V10P', 'qty': 4, 'days': 3}], on_date=self.on)
        self.assertEqual(q['lines'][0]['line_total'], '10200.00')
        self.assertEqual(q['lines'][0]['qty'], '4')
        self.assertEqual(q['lines'][0]['days'], '3')
        self.assertEqual(q['subtotal'], '10200.00')
        self.assertEqual(q['vat'], '1530.00')
        self.assertEqual(q['incl_vat'], '11730.00')

    def test_qty_and_days_accept_strings(self):
        """Clients send JSON strings — '4' and '3' must work like ints."""
        q = build_quote([{'code': 'DB-V10P', 'qty': '4', 'days': '3'}], on_date=self.on)
        self.assertEqual(q['lines'][0]['line_total'], '10200.00')

    def test_multi_line_subtotal(self):
        """Subtotal is the sum of the per-line totals."""
        q = build_quote([
            {'code': 'DB-V10P', 'qty': 4, 'days': 3},   # 10200
            {'code': 'DB-D40', 'qty': 1, 'days': 3},    # 4200
        ], on_date=self.on)
        self.assertEqual(q['lines'][1]['line_total'], '4200.00')
        self.assertEqual(q['subtotal'], '14400.00')
        self.assertEqual(q['vat'], '2160.00')
        self.assertEqual(q['incl_vat'], '16560.00')
        self._check_identity(q)

    def test_discount_then_vat_on_discounted_amount(self):
        """VAT must be computed on the DISCOUNTED amount, not the gross (10% off 14400)."""
        q = build_quote([
            {'code': 'DB-V10P', 'qty': 4, 'days': 3},
            {'code': 'DB-D40', 'qty': 1, 'days': 3},
        ], on_date=self.on, discount_pct='10')
        self.assertEqual(q['subtotal'], '14400.00')
        self.assertEqual(q['discount'], '1440.00')
        self.assertEqual(q['ex_vat'], '12960.00')
        self.assertEqual(q['vat'], '1944.00')       # 15% of 12960 — NOT 2160 (15% of gross)
        self.assertEqual(q['incl_vat'], '14904.00')
        self.assertEqual(q['discount_pct'], '10')
        self._check_identity(q)

    def test_vat_rate_default_and_explicit_zero(self):
        """vat_rate defaults to 0.15; explicit 0 → vat '0.00' and incl == ex."""
        q0 = build_quote([{'code': 'DB-V10P'}], on_date=self.on, vat_rate='0')
        self.assertEqual(q0['vat'], '0.00')
        self.assertEqual(q0['incl_vat'], q0['ex_vat'])
        self.assertEqual(q0['incl_vat'], '850.00')
        q_default = build_quote([{'code': 'DB-V10P'}], on_date=self.on)
        self.assertEqual(q_default['vat'], '127.50')

    def test_discount_100_gives_zero_ex_vat(self):
        """discount_pct=100 → ex_vat 0.00, vat 0.00, incl 0.00."""
        q = build_quote([{'code': 'DB-V10P'}], on_date=self.on, discount_pct=100)
        self.assertEqual(q['discount'], '850.00')
        self.assertEqual(q['ex_vat'], '0.00')
        self.assertEqual(q['vat'], '0.00')
        self.assertEqual(q['incl_vat'], '0.00')
        self._check_identity(q)

    def test_discount_out_of_range_rejected(self):
        """discount_pct outside 0..100 and negative vat_rate must raise ValueError."""
        with self.assertRaises(ValueError):
            build_quote([{'code': 'DB-V10P'}], on_date=self.on, discount_pct=101)
        with self.assertRaises(ValueError):
            build_quote([{'code': 'DB-V10P'}], on_date=self.on, discount_pct=-1)
        with self.assertRaises(ValueError):
            build_quote([{'code': 'DB-V10P'}], on_date=self.on, vat_rate=-0.15)

    def test_rounding_half_up_where_float_maths_drifts(self):
        """0.30 x 15% = 0.045 → HALF_UP gives 0.05 (float/banker's give 0.04). incl must be 0.35."""
        q = build_quote([{'code': 'CENT'}], on_date=self.on)
        self.assertEqual(q['subtotal'], '0.30')
        self.assertEqual(q['vat'], '0.05')
        self.assertEqual(q['incl_vat'], '0.35')
        self._check_identity(q)

    def test_rounding_discount_33_on_850(self):
        """33% of 850 = 280.50; VAT on 569.50 = 85.425 → 85.43 HALF_UP (float round gives 85.42)."""
        q = build_quote([{'code': 'DB-V10P'}], on_date=self.on, discount_pct='33')
        self.assertEqual(q['discount'], '280.50')
        self.assertEqual(q['ex_vat'], '569.50')
        self.assertEqual(q['vat'], '85.43')
        self.assertEqual(q['incl_vat'], '654.93')
        self._check_identity(q)

    def test_rounding_discount_7_5_on_875(self):
        """875 x 7.5% = 65.625 → 65.63 HALF_UP (banker's gives 65.62); ex 809.37; vat 121.41; incl 930.78."""
        seven = _item(code='SEVEN', name='seven', category='OTHER')
        _price(seven, '125.00', D(2025, 12, 3), None)
        q = build_quote([{'code': 'SEVEN', 'qty': 7}], on_date=self.on, discount_pct='7.5')
        self.assertEqual(q['subtotal'], '875.00')
        self.assertEqual(q['discount'], '65.63')
        self.assertEqual(q['ex_vat'], '809.37')
        self.assertEqual(q['vat'], '121.41')
        self.assertEqual(q['incl_vat'], '930.78')
        self._check_identity(q)

    def test_rounding_identity_holds_across_combinations(self):
        """ex_vat + vat == incl_vat EXACTLY for a spread of odd subtotals / discounts."""
        odd = _item(code='ODD', name='odd', category='OTHER')
        _price(odd, '333.33', D(2025, 12, 3), None)
        cases = [
            ([{'code': 'ODD', 'qty': 3}], '12.5', ('999.99', '125.00', '874.99', '131.25', '1006.24')),
            ([{'code': 'ODD', 'qty': 1}, {'code': 'CENT', 'qty': 1}], '0', ('333.63', '0.00', '333.63', '50.04', '383.67')),
            ([{'code': 'ODD', 'qty': 7, 'days': 2}], '3', ('4666.62', '140.00', '4526.62', '678.99', '5205.61')),
        ]
        for lines, disc, (sub, d, ex, vat, inc) in cases:
            with self.subTest(lines=lines, disc=disc):
                q = build_quote(lines, on_date=self.on, discount_pct=disc)
                self.assertEqual((q['subtotal'], q['discount'], q['ex_vat'], q['vat'], q['incl_vat']),
                                 (sub, d, ex, vat, inc))
                self._check_identity(q)

    def test_line_total_rounded_per_line_before_summing(self):
        """Per-line rounding: 0.30 x 0.5 x 1 = 0.15 exactly; 333.33 x 0.5 = 166.665 → 166.67 per line."""
        odd = _item(code='ODD', name='odd', category='OTHER')
        _price(odd, '333.33', D(2025, 12, 3), None)
        q = build_quote([{'code': 'ODD', 'qty': '0.5'}, {'code': 'ODD', 'qty': '0.5'}], on_date=self.on)
        self.assertEqual(q['lines'][0]['line_total'], '166.67')
        self.assertEqual(q['lines'][0]['qty'], '0.5')
        self.assertEqual(q['subtotal'], '333.34')
        self._check_identity(q)

    def test_unknown_code_does_not_abort(self):
        """Unknown code → priced=False, line_total '0.00', warning; other lines still priced; no raise."""
        q = build_quote([{'code': 'NOPE-1'}, {'code': 'DB-V10P'}], on_date=self.on)
        bad, good = q['lines']
        self.assertFalse(bad['priced'])
        self.assertIsNone(bad['unit_price'])
        self.assertEqual(bad['line_total'], '0.00')
        self.assertEqual(bad['code'], 'NOPE-1')
        self.assertTrue(good['priced'])
        self.assertEqual(q['subtotal'], '850.00')
        self.assertTrue(any('NOPE-1' in w for w in q['warnings']), q['warnings'])

    def test_item_with_no_price_row_does_not_abort(self):
        """An item with no valid price → priced=False / '0.00' / warning naming the code and date."""
        _item(code='NOPRICE', name='no price', category='OTHER')
        q = build_quote([{'code': 'NOPRICE'}, {'code': 'DB-V10P'}], on_date=self.on)
        ln = q['lines'][0]
        self.assertFalse(ln['priced'])
        self.assertEqual(ln['line_total'], '0.00')
        self.assertEqual(ln['name'], 'no price')  # item was found, just unpriced
        self.assertEqual(q['subtotal'], '850.00')
        self.assertTrue(any('NOPRICE' in w and '2026-02-01' in w for w in q['warnings']), q['warnings'])

    def test_price_not_yet_valid_on_quote_date_is_unpriced(self):
        """Quote dated before the price's valid_from must NOT pick up that price."""
        q = build_quote([{'code': 'DB-V10P'}], on_date=D(2025, 12, 2))
        self.assertFalse(q['lines'][0]['priced'])
        self.assertEqual(q['subtotal'], '0.00')

    def test_inactive_item_still_priced_but_warned(self):
        """Per the code: an inactive item IS priced and a warning '<code> is marked inactive' is added."""
        self.top.active = False
        self.top.save()
        q = build_quote([{'code': 'DB-V10P'}], on_date=self.on)
        self.assertTrue(q['lines'][0]['priced'])
        self.assertEqual(q['lines'][0]['line_total'], '850.00')
        self.assertTrue(any('DB-V10P' in w and 'inactive' in w for w in q['warnings']), q['warnings'])

    def test_qty_zero_or_days_zero_gives_zero_line_total(self):
        """qty=0 / days=0 → line_total 0.00 but the line is still priced (unit price shown)."""
        q = build_quote([{'code': 'DB-V10P', 'qty': 0}, {'code': 'DB-V10P', 'days': 0}], on_date=self.on)
        for ln in q['lines']:
            self.assertTrue(ln['priced'])
            self.assertEqual(ln['unit_price'], '850.00')
            self.assertEqual(ln['line_total'], '0.00')
        self.assertEqual(q['subtotal'], '0.00')
        self.assertEqual(q['incl_vat'], '0.00')

    def test_negative_qty_rejected(self):
        """Negative qty / days must raise ValueError (never a negative quote)."""
        with self.assertRaises(ValueError):
            build_quote([{'code': 'DB-V10P', 'qty': -1}], on_date=self.on)
        with self.assertRaises(ValueError):
            build_quote([{'code': 'DB-V10P', 'days': '-2'}], on_date=self.on)

    def test_garbage_qty_rejected_as_value_error(self):
        """'abc' / True / NaN / Infinity qty must raise ValueError (a 400 upstream), never another exception."""
        for junk in ('abc', True, 'NaN', 'Infinity', [1]):
            with self.subTest(qty=junk):
                with self.assertRaises(ValueError):
                    build_quote([{'code': 'DB-V10P', 'qty': junk}], on_date=self.on)

    def test_customer_pricing_flows_through_quote(self):
        """Aurras trade rate (680) must replace the list rate (850) in the totals."""
        q = build_quote([{'code': 'DB-V10P', 'qty': 2}, {'code': 'DB-D40'}], customer=self.aurras, on_date=self.on)
        self.assertEqual(q['lines'][0]['unit_price'], '680.00')
        self.assertEqual(q['lines'][0]['price_type'], 'TRADE')
        self.assertEqual(q['lines'][0]['line_total'], '1360.00')
        self.assertEqual(q['lines'][1]['unit_price'], '1400.00')   # no trade row → list
        self.assertEqual(q['lines'][1]['price_type'], 'LIST')
        self.assertEqual(q['subtotal'], '2760.00')
        self.assertEqual(q['customer_id'], self.aurras.contacts_id)
        self.assertEqual(q['customer_name'], 'AURRAS GROUP (PTY) LTD')
        self._check_identity(q)

    def test_other_customer_does_not_get_aurras_rate(self):
        """A different customer must be quoted at LIST, not at Aurras' trade rate."""
        other = _make_contact('Another Co')
        q = build_quote([{'code': 'DB-V10P'}], customer=other, on_date=self.on)
        self.assertEqual(q['lines'][0]['unit_price'], '850.00')
        self.assertEqual(q['lines'][0]['price_type'], 'LIST')

    def test_codes_are_case_insensitive_in_quote(self):
        """'db-v10p' must resolve the same item as 'DB-V10P' and be echoed upper-cased."""
        q = build_quote([{'code': ' db-v10p '}], on_date=self.on)
        self.assertTrue(q['lines'][0]['priced'])
        self.assertEqual(q['lines'][0]['code'], 'DB-V10P')

    def test_build_quote_persists_nothing(self):
        """Pure function: no rows created or modified by quoting."""
        before_prices = PriceListPrice.objects.count()
        before_items = PriceListItem.objects.count()
        snapshot = list(PriceListPrice.objects.order_by('id').values_list('id', 'price', 'valid_from', 'valid_to'))
        build_quote([{'code': 'DB-V10P', 'qty': 3}, {'code': 'NOPE'}, {'code': 'DB-D40'}],
                    customer=self.aurras, on_date=self.on, discount_pct='10')
        self.assertEqual(PriceListPrice.objects.count(), before_prices)
        self.assertEqual(PriceListItem.objects.count(), before_items)
        self.assertEqual(list(PriceListPrice.objects.order_by('id').values_list('id', 'price', 'valid_from', 'valid_to')),
                         snapshot)

    def test_empty_line_dict_and_none_line_do_not_crash(self):
        """{} and None lines must come back unpriced with a warning, not raise."""
        q = build_quote([{}, None, {'code': 'DB-V10P'}], on_date=self.on)
        self.assertEqual(len(q['lines']), 3)
        self.assertFalse(q['lines'][0]['priced'])
        self.assertFalse(q['lines'][1]['priced'])
        self.assertTrue(q['lines'][2]['priced'])
        self.assertEqual(q['subtotal'], '850.00')


# =========================================================================== #
# 6. REST API
# =========================================================================== #
class ApiTestBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        # Surface unhandled exceptions as a 500 status so "must be 400 not 500" assertions read cleanly.
        self.client.raise_request_exception = False
        self.top = _item(code='DB-V10P', name='d&b V10P point-source top', category='PA',
                         description='point source PA top box')
        self.amp = _item(code='DB-D40', name='d&b D40 amplifier', category='AMP', description='4ch amp')
        self.old = _item(code='OLD-THING', name='retired thing', category='OTHER', active=False)
        _price(self.top, '850.00', D(2025, 12, 3), None)
        _price(self.amp, '1400.00', D(2025, 12, 3), None)
        _price(self.old, '10.00', D(2025, 12, 3), None)
        self.aurras = _make_contact('AURRAS GROUP (PTY) LTD')
        _price(self.top, '680.00', D(2025, 12, 3), None, price_type='TRADE', customer=self.aurras)
        self.items_url = reverse('pricelist:items')

    def _url(self, name, code=None):
        return reverse(f'pricelist:{name}', kwargs={'code': code}) if code else reverse(f'pricelist:{name}')


class ItemsListApiTests(ApiTestBase):
    def test_url_mounts_at_api_pricelist(self):
        """The app must be mounted at /api/pricelist/ (the MCP server hard-codes this path)."""
        self.assertEqual(self.items_url, '/api/pricelist/items/')
        self.assertEqual(self.client.get('/api/pricelist/items/').status_code, 200)

    def test_list_returns_items_with_current_price(self):
        """GET /items/ returns all items, each with current_price as a 2-dp string."""
        r = self.client.get(self.items_url, {'date': '2026-02-01'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['count'], 3)
        by_code = {i['code']: i for i in body['items']}
        self.assertEqual(by_code['DB-V10P']['current_price'], '850.00')
        self.assertEqual(by_code['DB-V10P']['current_price_valid_from'], '2025-12-03')
        self.assertEqual(by_code['DB-D40']['current_price'], '1400.00')
        self.assertEqual(by_code['DB-V10P']['price_count'], 2)   # LIST + Aurras TRADE
        self.assertIsNone(body['customer'])
        self.assertEqual(body['categories'], ['AMP', 'OTHER', 'PA'])

    def test_category_filter(self):
        """?category=pa (any case) returns only PA items."""
        r = self.client.get(self.items_url, {'category': 'pa'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([i['code'] for i in r.json()['items']], ['DB-V10P'])

    def test_active_filter(self):
        """?active=0 returns only inactive items; ?active=1 excludes them; junk → 400."""
        r = self.client.get(self.items_url, {'active': '0'})
        self.assertEqual([i['code'] for i in r.json()['items']], ['OLD-THING'])
        r = self.client.get(self.items_url, {'active': 'true'})
        self.assertNotIn('OLD-THING', [i['code'] for i in r.json()['items']])
        self.assertEqual(r.json()['count'], 2)
        self.assertEqual(self.client.get(self.items_url, {'active': 'maybe'}).status_code, 400)

    def test_q_matches_code_name_and_description(self):
        """?q= must search code, name AND description (case-insensitive)."""
        self.assertEqual([i['code'] for i in self.client.get(self.items_url, {'q': 'db-d4'}).json()['items']],
                         ['DB-D40'])                                                     # code
        self.assertEqual([i['code'] for i in self.client.get(self.items_url, {'q': 'Amplifier'}).json()['items']],
                         ['DB-D40'])                                                     # name
        self.assertEqual([i['code'] for i in self.client.get(self.items_url, {'q': 'point source'}).json()['items']],
                         ['DB-V10P'])                                                    # description
        self.assertEqual(self.client.get(self.items_url, {'q': 'zzz-nothing'}).json()['count'], 0)

    def test_customer_param_adds_customer_price(self):
        """?customer=<id> adds customer_price (trade rate where one exists, list otherwise)."""
        r = self.client.get(self.items_url, {'customer': self.aurras.contacts_id, 'date': '2026-02-01'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['customer'], {'contacts_id': self.aurras.contacts_id, 'name': 'AURRAS GROUP (PTY) LTD'})
        by_code = {i['code']: i for i in body['items']}
        self.assertEqual(by_code['DB-V10P']['customer_price'], '680.00')
        self.assertEqual(by_code['DB-V10P']['customer_price_type'], 'TRADE')
        self.assertEqual(by_code['DB-V10P']['current_price'], '850.00')   # list price still shown
        self.assertEqual(by_code['DB-D40']['customer_price'], '1400.00')
        self.assertEqual(by_code['DB-D40']['customer_price_type'], 'LIST')

    def test_customer_param_by_name(self):
        """?customer=<name> (case-insensitive / partial) resolves to the contact."""
        r = self.client.get(self.items_url, {'customer': 'aurras', 'date': '2026-02-01'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['customer']['contacts_id'], self.aurras.contacts_id)

    def test_ambiguous_customer_name_is_400(self):
        """A name matching several contacts must be a 400 listing candidates, never a silent guess."""
        _make_contact('AURRAS EVENTS')
        r = self.client.get(self.items_url, {'customer': 'aurras'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('ambiguous', r.json()['detail'])

    def test_unknown_customer_on_list_is_400_not_silently_ignored(self):
        """An unknown ?customer= must be a 400 (as /quote/ and /prices/ do) — silently dropping it
        makes the caller think the LIST prices are that customer's prices."""
        r = self.client.get(self.items_url, {'customer': 'NO SUCH CUSTOMER XYZ'})
        self.assertEqual(r.status_code, 400)

    def test_bad_date_is_400(self):
        """?date=not-a-date → 400, not 500."""
        r = self.client.get(self.items_url, {'date': 'not-a-date'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('date', r.json()['detail'])


class ItemDetailAndPriceApiTests(ApiTestBase):
    def test_item_detail_get(self):
        """GET /items/<code>/ returns the item dict."""
        r = self.client.get(self._url('item_detail', 'DB-V10P'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['code'], 'DB-V10P')
        self.assertEqual(r.json()['current_price'], '850.00')

    def test_lowercase_code_in_url_resolves(self):
        """Codes are upper-cased on save — the URL lookup must be case-insensitive."""
        r = self.client.get(self._url('item_detail', 'db-v10p'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['code'], 'DB-V10P')
        r = self.client.get(self._url('item_price', 'db-v10p'), {'date': '2026-02-01'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['price'], '850.00')

    def test_unknown_code_is_404(self):
        """Unknown code on every per-item endpoint → 404 (not 500)."""
        for name in ('item_detail', 'item_price', 'item_prices'):
            with self.subTest(name=name):
                r = self.client.get(self._url(name, 'NOPE-999'))
                self.assertEqual(r.status_code, 404)
        r = self.client.post(self._url('item_prices', 'NOPE-999'), {'price': '1', 'valid_from': '2026-01-01'},
                             format='json')
        self.assertEqual(r.status_code, 404)
        r = self.client.patch(self._url('item_detail', 'NOPE-999'), {'name': 'x'}, format='json')
        self.assertEqual(r.status_code, 404)

    def test_price_endpoint_resolves(self):
        """GET /items/<code>/price/?date=&customer=&type= returns the resolved dict."""
        r = self.client.get(self._url('item_price', 'DB-V10P'),
                            {'date': '2026-02-01', 'customer': self.aurras.contacts_id, 'type': 'LIST'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['resolved'])
        self.assertEqual(body['price'], '680.00')
        self.assertEqual(body['price_type'], 'TRADE')
        self.assertEqual(body['date'], '2026-02-01')
        self.assertEqual(body['customer_id'], self.aurras.contacts_id)
        self.assertEqual(body['customer_name'], 'AURRAS GROUP (PTY) LTD')
        # without customer, the list price
        r = self.client.get(self._url('item_price', 'DB-V10P'), {'date': '2026-02-01'})
        self.assertEqual(r.json()['price'], '850.00')
        self.assertEqual(r.json()['price_type'], 'LIST')

    def test_price_endpoint_before_valid_from_is_unresolved_200(self):
        """A date before any price: 200 with resolved=False and price null (not 404)."""
        r = self.client.get(self._url('item_price', 'DB-V10P'), {'date': '2025-01-01'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['resolved'])
        self.assertIsNone(r.json()['price'])

    def test_price_endpoint_trade_fallback_flag(self):
        """?type=TRADE with no trade lane → LIST price and fallback_to_list true."""
        r = self.client.get(self._url('item_price', 'DB-D40'), {'date': '2026-02-01', 'type': 'trade'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['price'], '1400.00')
        self.assertTrue(r.json()['fallback_to_list'])
        self.assertEqual(r.json()['requested_price_type'], 'TRADE')

    def test_price_endpoint_bad_inputs_are_400(self):
        """bad date / bad type → 400, never 500."""
        self.assertEqual(self.client.get(self._url('item_price', 'DB-V10P'), {'date': '2026-13-45'}).status_code, 400)
        self.assertEqual(self.client.get(self._url('item_price', 'DB-V10P'), {'date': 'yesterday'}).status_code, 400)
        r = self.client.get(self._url('item_price', 'DB-V10P'), {'type': 'WHOLESALE'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('LIST', r.json()['detail'])

    def test_price_endpoint_unknown_customer_is_400(self):
        """An unknown ?customer= on /price/ must be 400 — returning the LIST price with resolved=true
        would be quoted to the wrong party as 'their' price."""
        r = self.client.get(self._url('item_price', 'DB-V10P'), {'customer': 'NO SUCH CUSTOMER XYZ'})
        self.assertEqual(r.status_code, 400)

    def test_patch_updates_fields_but_never_prices(self):
        """PATCH edits whitelisted fields; price/valid_from/code in the body → 400."""
        url = self._url('item_detail', 'DB-V10P')
        r = self.client.patch(url, {'qty_owned': 4, 'notes': 'two more bought'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['qty_owned'], 4)
        self.assertEqual(r.json()['notes'], 'two more bought')
        self.assertEqual(r.json()['current_price'], '850.00')   # untouched
        self.assertEqual(self.client.patch(url, {'price': '1'}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(url, {'valid_from': '2026-01-01'}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(url, {'code': 'NEW-CODE'}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(url, {'qty_owned': -1}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(url, {'category': 'NOPE'}, format='json').status_code, 400)
        self.top.refresh_from_db()
        self.assertEqual(self.top.code, 'DB-V10P')
        self.assertEqual(PriceListPrice.objects.filter(item=self.top).count(), 2)


class ItemsCreateApiTests(ApiTestBase):
    def test_post_creates_item_201_and_uppercases_code(self):
        """POST /items/ creates (201), code upper-cased/stripped, category validated."""
        r = self.client.post(self.items_url, {'code': ' pio-cdj3000 ', 'name': 'Pioneer CDJ-3000', 'category': 'dj',
                                              'unit': 'day', 'qty_owned': 2}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertTrue(body['created'])
        self.assertEqual(body['item']['code'], 'PIO-CDJ3000')
        self.assertEqual(body['item']['category'], 'DJ')
        self.assertEqual(body['item']['unit'], 'DAY')
        self.assertEqual(body['item']['qty_owned'], 2)
        self.assertIsNone(body['price'])
        self.assertTrue(PriceListItem.objects.filter(code='PIO-CDJ3000').exists())

    def test_post_with_opening_price_creates_price_row(self):
        """POST /items/ with price+valid_from creates the item AND its opening LIST price."""
        r = self.client.post(self.items_url, {'code': 'NEW-1', 'name': 'New', 'category': 'OTHER',
                                              'price': '99.99', 'valid_from': '2026-01-01'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()['price']['price'], '99.99')
        self.assertEqual(r.json()['price']['valid_from'], '2026-01-01')
        self.assertIsNone(r.json()['price']['valid_to'])
        self.assertEqual(r.json()['item']['current_price'], '99.99')

    def test_post_price_without_valid_from_is_400_and_no_item_left_behind(self):
        """price without valid_from (or vice versa) → 400 and the item must NOT be created."""
        r = self.client.post(self.items_url, {'code': 'HALF-1', 'name': 'Half', 'price': '10'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(PriceListItem.objects.filter(code='HALF-1').exists())
        r = self.client.post(self.items_url, {'code': 'HALF-2', 'name': 'Half', 'valid_from': '2026-01-01'},
                             format='json')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(PriceListItem.objects.filter(code='HALF-2').exists())

    def test_post_garbage_opening_price_is_400_and_no_item_left_behind(self):
        """price='abc' with valid_from → 400 (not 500) and no orphan item."""
        r = self.client.post(self.items_url, {'code': 'JUNK-1', 'name': 'Junk', 'price': 'abc',
                                              'valid_from': '2026-01-01'}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(PriceListItem.objects.filter(code='JUNK-1').exists())
        r = self.client.post(self.items_url, {'code': 'JUNK-2', 'name': 'Junk', 'price': '-5',
                                              'valid_from': '2026-01-01'}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(PriceListItem.objects.filter(code='JUNK-2').exists())

    def test_post_duplicate_code_is_409_then_replace_true_is_200(self):
        """Same code → 409; replace=true → 200 with created=false and the fields updated."""
        r = self.client.post(self.items_url, {'code': 'db-v10p', 'name': 'renamed', 'category': 'PA'}, format='json')
        self.assertEqual(r.status_code, 409)
        self.top.refresh_from_db()
        self.assertEqual(self.top.name, 'd&b V10P point-source top')   # untouched
        r = self.client.post(self.items_url, {'code': 'DB-V10P', 'name': 'renamed', 'category': 'PA',
                                              'replace': True, 'qty_owned': 9}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()['created'])
        self.assertEqual(r.json()['item']['name'], 'renamed')
        self.assertEqual(r.json()['item']['qty_owned'], 9)
        self.top.refresh_from_db()
        self.assertEqual(self.top.name, 'renamed')
        # replace must not touch the price history
        self.assertEqual(PriceListPrice.objects.filter(item=self.top).count(), 2)

    def test_post_invalid_category_lists_allowed_values(self):
        """Invalid category → 400 and the detail names the allowed values."""
        r = self.client.post(self.items_url, {'code': 'X-1', 'name': 'X', 'category': 'DRONES'}, format='json')
        self.assertEqual(r.status_code, 400)
        detail = r.json()['detail']
        for allowed in ('PA', 'AMP', 'DJ', 'LIGHTING', 'OTHER'):
            self.assertIn(allowed, detail)
        self.assertFalse(PriceListItem.objects.filter(code='X-1').exists())

    def test_post_missing_code_or_name_is_400(self):
        """code and name are required."""
        self.assertEqual(self.client.post(self.items_url, {'name': 'no code'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(self.items_url, {'code': 'NO-NAME'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(self.items_url, {'code': '  ', 'name': 'blank'}, format='json').status_code, 400)

    def test_post_invalid_unit_and_qty_are_400(self):
        """unit outside choices / non-integer qty_owned → 400."""
        self.assertEqual(self.client.post(self.items_url, {'code': 'U-1', 'name': 'u', 'unit': 'HOUR'},
                                          format='json').status_code, 400)
        self.assertEqual(self.client.post(self.items_url, {'code': 'U-2', 'name': 'u', 'qty_owned': 'two'},
                                          format='json').status_code, 400)


class ItemPricesApiTests(ApiTestBase):
    def test_get_prices_history_newest_first(self):
        """GET /items/<code>/prices/ lists the lane history, newest first, money as strings."""
        add_price(self.top, price='900', valid_from=D(2026, 3, 1))
        r = self.client.get(self._url('item_prices', 'DB-V10P'))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['code'], 'DB-V10P')
        self.assertEqual(body['count'], 3)
        self.assertEqual(body['prices'][0]['price'], '900.00')
        self.assertEqual(body['prices'][0]['valid_from'], '2026-03-01')
        self.assertIsNone(body['prices'][0]['valid_to'])
        closed = [p for p in body['prices'] if p['price_type'] == 'LIST' and p['valid_from'] == '2025-12-03'][0]
        self.assertEqual(closed['valid_to'], '2026-02-28')
        trade = [p for p in body['prices'] if p['price_type'] == 'TRADE'][0]
        self.assertEqual(trade['customer_id'], self.aurras.contacts_id)
        self.assertEqual(trade['customer_name'], 'AURRAS GROUP (PTY) LTD')

    def test_post_price_adds_and_closes_previous(self):
        """POST /items/<code>/prices/ → 201, new row open, closed_previous shows valid_to = new-1."""
        r = self.client.post(self._url('item_prices', 'DB-V10P'),
                             {'price': '900', 'valid_from': '2026-03-01', 'set_by': 'test', 'note': 'rate rise'},
                             format='json')
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body['price']['price'], '900.00')
        self.assertEqual(body['price']['valid_from'], '2026-03-01')
        self.assertIsNone(body['price']['valid_to'])
        self.assertEqual(body['price']['price_type'], 'LIST')
        self.assertEqual(body['price']['set_by'], 'test')
        self.assertEqual(body['price']['note'], 'rate rise')
        self.assertIsNotNone(body['closed_previous'])
        self.assertEqual(body['closed_previous']['price'], '850.00')
        self.assertEqual(body['closed_previous']['valid_to'], '2026-02-28')
        # and the resolver agrees
        self.assertEqual(self.client.get(self._url('item_price', 'DB-V10P'), {'date': '2026-02-28'}).json()['price'],
                         '850.00')
        self.assertEqual(self.client.get(self._url('item_price', 'DB-V10P'), {'date': '2026-03-01'}).json()['price'],
                         '900.00')

    def test_post_price_first_in_lane_has_no_closed_previous(self):
        """A TRADE price for a new customer: 201, closed_previous null, LIST row untouched."""
        cust = _make_contact('New Trade Co')
        r = self.client.post(self._url('item_prices', 'DB-D40'),
                             {'price': '1120', 'valid_from': '2026-01-01', 'price_type': 'trade',
                              'customer': cust.contacts_id}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIsNone(r.json()['closed_previous'])
        self.assertEqual(r.json()['price']['price_type'], 'TRADE')
        self.assertEqual(r.json()['price']['customer_id'], cust.contacts_id)
        self.assertEqual(r.json()['price']['customer_name'], 'New Trade Co')
        self.assertIsNone(PriceListPrice.objects.get(item=self.amp, price_type='LIST').valid_to)

    def test_post_price_back_dated_is_400_with_message(self):
        """Back-dated price → 400 with a human message, and nothing written."""
        r = self.client.post(self._url('item_prices', 'DB-V10P'), {'price': '800', 'valid_from': '2025-01-01'},
                             format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('back-date', r.json()['detail'])
        self.assertEqual(PriceListPrice.objects.filter(item=self.top, price_type='LIST').count(), 1)

    def test_post_price_missing_fields_is_400(self):
        """Missing price or valid_from → 400."""
        url = self._url('item_prices', 'DB-V10P')
        self.assertEqual(self.client.post(url, {'price': '900'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(url, {'valid_from': '2026-03-01'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(url, {}, format='json').status_code, 400)

    def test_post_price_garbage_values_are_400_not_500(self):
        """price='abc' / 'NaN' / 14-digit, bad date, bad type, negative → 400 every time, never 500."""
        url = self._url('item_prices', 'DB-V10P')
        cases = [
            {'price': 'abc', 'valid_from': '2026-03-01'},
            {'price': 'NaN', 'valid_from': '2026-03-01'},
            {'price': 'Infinity', 'valid_from': '2026-03-01'},
            {'price': '99999999999999', 'valid_from': '2026-03-01'},
            {'price': '-1', 'valid_from': '2026-03-01'},
            {'price': '900', 'valid_from': '03/01/2026'},
            {'price': '900', 'valid_from': '2026-03-01', 'price_type': 'WHOLESALE'},
            {'price': True, 'valid_from': '2026-03-01'},
        ]
        for body in cases:
            with self.subTest(body=body):
                r = self.client.post(url, body, format='json')
                self.assertEqual(r.status_code, 400, f'{body} -> {r.status_code} {r.content[:200]}')
        self.assertEqual(PriceListPrice.objects.filter(item=self.top, price_type='LIST').count(), 1)
        self.assertIsNone(PriceListPrice.objects.get(item=self.top, price_type='LIST').valid_to)

    def test_post_price_unknown_customer_is_400(self):
        """customer that does not resolve → 400 (must not silently create a general price)."""
        r = self.client.post(self._url('item_prices', 'DB-V10P'),
                             {'price': '700', 'valid_from': '2026-03-01', 'customer': 'NOBODY INC'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PriceListPrice.objects.filter(item=self.top).count(), 2)

    def test_post_same_valid_from_twice_is_idempotent_via_api(self):
        """Re-POSTing the same valid_from updates in place: count unchanged, closed_previous null."""
        url = self._url('item_prices', 'DB-V10P')
        r1 = self.client.post(url, {'price': '900', 'valid_from': '2026-03-01'}, format='json')
        self.assertEqual(r1.status_code, 201)
        r2 = self.client.post(url, {'price': '910', 'valid_from': '2026-03-01'}, format='json')
        self.assertIn(r2.status_code, (200, 201))
        self.assertIsNone(r2.json()['closed_previous'])
        self.assertEqual(r2.json()['price']['id'], r1.json()['price']['id'])
        self.assertEqual(r2.json()['price']['price'], '910.00')
        self.assertEqual(PriceListPrice.objects.filter(item=self.top, price_type='LIST').count(), 2)


class QuoteApiTests(ApiTestBase):
    def test_quote_happy_path(self):
        """POST /quote/ returns the full quote shape with string money."""
        r = self.client.post(self._url('quote'), {
            'lines': [{'code': 'DB-V10P', 'qty': 4, 'days': 3}, {'code': 'DB-D40', 'qty': 1, 'days': 3}],
            'date': '2026-02-01', 'discount_pct': '10',
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body['subtotal'], '14400.00')
        self.assertEqual(body['discount'], '1440.00')
        self.assertEqual(body['ex_vat'], '12960.00')
        self.assertEqual(body['vat'], '1944.00')
        self.assertEqual(body['incl_vat'], '14904.00')
        self.assertEqual(body['date'], '2026-02-01')
        self.assertEqual(body['warnings'], [])
        self.assertIsInstance(body['subtotal'], str)   # money is a STRING in JSON, never a float

    def test_quote_with_customer_by_name_and_vat_zero(self):
        """customer by name flows through; vat_rate=0 gives vat 0.00."""
        r = self.client.post(self._url('quote'), {
            'lines': [{'code': 'db-v10p', 'qty': 2}], 'customer': 'AURRAS GROUP (PTY) LTD',
            'date': '2026-02-01', 'vat_rate': 0,
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body['customer_id'], self.aurras.contacts_id)
        self.assertEqual(body['lines'][0]['unit_price'], '680.00')
        self.assertEqual(body['subtotal'], '1360.00')
        self.assertEqual(body['vat'], '0.00')
        self.assertEqual(body['incl_vat'], '1360.00')

    def test_quote_unknown_code_is_200_with_warning(self):
        """An unknown code in the quote → 200, warning, other lines priced."""
        r = self.client.post(self._url('quote'), {'lines': [{'code': 'NOPE'}, {'code': 'DB-V10P'}],
                                                   'date': '2026-02-01'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['lines'][0]['priced'])
        self.assertEqual(r.json()['subtotal'], '850.00')
        self.assertTrue(r.json()['warnings'])

    def test_quote_bad_lines_are_400(self):
        """lines missing / [] / not a list / non-dict entries → 400."""
        url = self._url('quote')
        for body in ({}, {'lines': []}, {'lines': 'DB-V10P'}, {'lines': {'code': 'DB-V10P'}},
                     {'lines': ['DB-V10P']}, {'lines': [1, 2]}, {'lines': None}):
            with self.subTest(body=body):
                r = self.client.post(url, body, format='json')
                self.assertEqual(r.status_code, 400, f'{body} -> {r.status_code}')

    def test_quote_bad_scalars_are_400_not_500(self):
        """bad date / discount / vat / qty / unknown customer → 400, never 500."""
        url = self._url('quote')
        good = [{'code': 'DB-V10P'}]
        cases = [
            {'lines': good, 'date': 'not-a-date'},
            {'lines': good, 'discount_pct': 'abc'},
            {'lines': good, 'discount_pct': 101},
            {'lines': good, 'discount_pct': -1},
            {'lines': good, 'vat_rate': 'abc'},
            {'lines': good, 'vat_rate': -0.15},
            {'lines': good, 'customer': 'NOBODY INC'},
            {'lines': [{'code': 'DB-V10P', 'qty': -1}]},
            {'lines': [{'code': 'DB-V10P', 'qty': 'abc'}]},
            {'lines': [{'code': 'DB-V10P', 'qty': 'NaN'}]},
            {'lines': [{'code': 'DB-V10P', 'days': 'Infinity'}]},
            {'lines': [{'code': 'DB-V10P', 'qty': True}]},
        ]
        for body in cases:
            with self.subTest(body=body):
                r = self.client.post(url, body, format='json')
                self.assertEqual(r.status_code, 400, f'{body} -> {r.status_code} {r.content[:200]}')

    def test_quote_persists_nothing_via_api(self):
        """POST /quote/ must not write any pricelist rows."""
        before = PriceListPrice.objects.count()
        self.client.post(self._url('quote'), {'lines': [{'code': 'DB-V10P'}], 'customer': self.aurras.contacts_id},
                         format='json')
        self.assertEqual(PriceListPrice.objects.count(), before)

    def test_quote_get_is_not_allowed(self):
        """Only POST on /quote/."""
        self.assertEqual(self.client.get(self._url('quote')).status_code, 405)


class ExportApiTests(ApiTestBase):
    def _rows(self, response):
        text = response.content.decode('utf-8')
        return list(csv.reader(io.StringIO(text)))

    def test_export_csv_headers_and_content(self):
        """GET /export/ → 200 text/csv attachment; parsed CSV has the item code AND its price in the right columns."""
        r = self.client.get(self._url('export'), {'date': '2026-02-01'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r['Content-Type'].startswith('text/csv'), r['Content-Type'])
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertIn('klikk-pricelist-2026-02-01.csv', r['Content-Disposition'])
        rows = self._rows(r)
        header = rows[0]
        self.assertIn('code', header)
        self.assertIn('current_price_ex_vat', header)
        self.assertIn('price_valid_from', header)
        self.assertIn('customer_price', header)
        self.assertIn('active', header)
        by_code = {row[header.index('code')]: row for row in rows[1:]}
        self.assertIn('DB-V10P', by_code)
        self.assertIn('DB-D40', by_code)
        self.assertIn('OLD-THING', by_code)   # inactive included by default
        self.assertEqual(by_code['DB-V10P'][header.index('current_price_ex_vat')], '850.00')
        self.assertEqual(by_code['DB-V10P'][header.index('price_valid_from')], '2025-12-03')
        self.assertEqual(by_code['DB-D40'][header.index('current_price_ex_vat')], '1400.00')
        self.assertEqual(by_code['DB-V10P'][header.index('customer_price')], '')   # no customer asked
        self.assertEqual(by_code['OLD-THING'][header.index('active')], '0')
        self.assertEqual(by_code['DB-V10P'][header.index('active')], '1')

    def test_export_customer_and_active_filters(self):
        """?customer= fills customer_price (trade where it exists); ?active=1 drops inactive rows."""
        r = self.client.get(self._url('export'), {'date': '2026-02-01', 'customer': self.aurras.contacts_id,
                                                  'active': '1'})
        self.assertEqual(r.status_code, 200)
        rows = self._rows(r)
        header = rows[0]
        by_code = {row[header.index('code')]: row for row in rows[1:]}
        self.assertNotIn('OLD-THING', by_code)
        self.assertEqual(by_code['DB-V10P'][header.index('customer_price')], '680.00')
        self.assertEqual(by_code['DB-V10P'][header.index('current_price_ex_vat')], '850.00')
        self.assertEqual(by_code['DB-D40'][header.index('customer_price')], '1400.00')

    def test_export_date_before_prices_gives_blank_price(self):
        """An export dated before any price must show a blank price, not today's price."""
        r = self.client.get(self._url('export'), {'date': '2025-01-01'})
        rows = self._rows(r)
        header = rows[0]
        by_code = {row[header.index('code')]: row for row in rows[1:]}
        self.assertEqual(by_code['DB-V10P'][header.index('current_price_ex_vat')], '')
        self.assertEqual(by_code['DB-V10P'][header.index('price_valid_from')], '')

    def test_export_bad_params_are_400(self):
        """bad date / bad active → 400 JSON, not a half-written CSV or a 500."""
        self.assertEqual(self.client.get(self._url('export'), {'date': 'nope'}).status_code, 400)
        self.assertEqual(self.client.get(self._url('export'), {'active': 'maybe'}).status_code, 400)

    def test_export_escapes_commas_and_quotes_in_notes(self):
        """Notes containing commas / quotes must survive a CSV round-trip (csv.writer quoting)."""
        self.top.notes = 'bought 2, "as new", from Cape Town'
        self.top.save()
        r = self.client.get(self._url('export'), {'date': '2026-02-01'})
        rows = self._rows(r)
        header = rows[0]
        by_code = {row[header.index('code')]: row for row in rows[1:]}
        self.assertEqual(by_code['DB-V10P'][header.index('notes')], 'bought 2, "as new", from Cape Town')
        self.assertTrue(all(len(row) == len(header) for row in rows[1:]))


# =========================================================================== #
# 7. Model behaviour
# =========================================================================== #
class ModelTests(TestCase):
    def test_code_is_uppercased_and_stripped_on_save(self):
        """'  db-v10p ' must be stored as 'DB-V10P' and be unique against that."""
        item = PriceListItem.objects.create(code='  db-v10p ', name='x')
        item.refresh_from_db()
        self.assertEqual(item.code, 'DB-V10P')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PriceListItem.objects.create(code='DB-v10P', name='dup')
        self.assertEqual(PriceListItem.objects.count(), 1)

    def test_check_constraint_rejects_valid_to_before_valid_from(self):
        """valid_to < valid_from must be rejected by the DB (pricelist_price_valid_range)."""
        item = _item()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _price(item, '1.00', D(2026, 1, 10), D(2026, 1, 9))
        self.assertEqual(PriceListPrice.objects.count(), 0)
        # equal dates (one-day price) are allowed
        row = _price(item, '1.00', D(2026, 1, 10), D(2026, 1, 10))
        self.assertIsNotNone(row.pk)
        self.assertEqual(get_price(item, on_date=D(2026, 1, 10)).id, row.id)
        self.assertIsNone(get_price(item, on_date=D(2026, 1, 11)))

    def test_deleting_item_cascades_prices(self):
        """Deleting an item removes its price rows (CASCADE), leaving no orphans."""
        item = _item()
        _price(item, '1.00', D(2026, 1, 1), None)
        item.delete()
        self.assertEqual(PriceListPrice.objects.count(), 0)

    def test_deleting_customer_keeps_price_row_as_general_override(self):
        """customer FK is SET_NULL — deleting the contact must NOT delete the price row.
        (Note: a customer TRADE row with customer=NULL then becomes a GENERAL trade row.)"""
        item = _item()
        cust = _make_contact('Short Lived Co')
        row = _price(item, '1.00', D(2026, 1, 1), None, price_type='TRADE', customer=cust)
        cust.delete()
        row.refresh_from_db()
        self.assertIsNone(row.customer_id)
        self.assertEqual(PriceListPrice.objects.count(), 1)

    def test_str_does_not_crash(self):
        """__str__ on both models must not raise for open and closed rows."""
        item = _item()
        open_row = _price(item, '1.00', D(2026, 1, 1), None)
        closed_row = _price(item, '2.00', D(2025, 1, 1), D(2025, 12, 31))
        self.assertIn('DB-V10P', str(item))
        self.assertIn('open', str(open_row))
        self.assertIn('2025-12-31', str(closed_row))


# =========================================================================== #
# 8. Seed command — idempotency only (tolerates an empty Xero mirror)
# =========================================================================== #
class SeedCommandIdempotencyTests(TestCase):
    def _run(self):
        out, err = io.StringIO(), io.StringIO()
        call_command('seed_pricelist', stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_seed_twice_is_idempotent(self):
        """Running the seed twice must not duplicate items or prices, nor close any row."""
        self._run()
        items_1 = PriceListItem.objects.count()
        prices_1 = PriceListPrice.objects.count()
        self.assertGreater(items_1, 0)
        self.assertGreater(prices_1, 0)
        self.assertEqual(PriceListPrice.objects.filter(valid_to__isnull=False).count(), 0)
        def _snapshot():
            return sorted(str(t) for t in PriceListPrice.objects.values_list(
                'item__code', 'price_type', 'customer_id', 'valid_from', 'valid_to', 'price'))

        snapshot_1 = _snapshot()
        self._run()
        self.assertEqual(PriceListItem.objects.count(), items_1)
        self.assertEqual(PriceListPrice.objects.count(), prices_1)
        self.assertEqual(PriceListPrice.objects.filter(valid_to__isnull=False).count(), 0)
        snapshot_2 = _snapshot()
        self.assertEqual(snapshot_1, snapshot_2)
        # every (item, lane, valid_from) is unique — no duplicate prices
        lanes = PriceListPrice.objects.values_list('item_id', 'price_type', 'customer_id', 'valid_from')
        self.assertEqual(len(list(lanes)), len(set(lanes)))
        # all seeded codes are upper-case and every item has exactly one open general LIST price
        for item in PriceListItem.objects.all():
            self.assertEqual(item.code, item.code.upper())
            self.assertEqual(item.prices.filter(price_type='LIST', customer__isnull=True, valid_to__isnull=True).count(), 1)

    def test_seed_preserves_deactivated_item(self):
        """An item MC deactivated must stay inactive after a re-seed (active only set on create)."""
        self._run()
        item = PriceListItem.objects.order_by('code').first()
        item.active = False
        item.save()
        self._run()
        item.refresh_from_db()
        self.assertFalse(item.active)

    def test_seed_dry_run_writes_nothing(self):
        """--dry-run must leave the tables empty."""
        out, err = io.StringIO(), io.StringIO()
        call_command('seed_pricelist', '--dry-run', stdout=out, stderr=err)
        self.assertEqual(PriceListItem.objects.count(), 0)
        self.assertEqual(PriceListPrice.objects.count(), 0)
        self.assertIn('DRY RUN', out.getvalue())

    def test_seed_only_filter(self):
        """--only X seeds exactly that item (and no others)."""
        out, err = io.StringIO(), io.StringIO()
        call_command('seed_pricelist', '--only', 'nonexistent-code', stdout=out, stderr=err)
        self.assertEqual(PriceListItem.objects.count(), 0)
        self._run()
        first = PriceListItem.objects.order_by('code').first().code
        PriceListItem.objects.all().delete()
        call_command('seed_pricelist', '--only', first.lower(), stdout=out, stderr=err)
        self.assertEqual(list(PriceListItem.objects.values_list('code', flat=True)), [first])
