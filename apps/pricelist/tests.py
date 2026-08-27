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
from django.test import TestCase, override_settings
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

    Created with ``bulk_create``, matching what
    ``XeroContactsModelManager.create_contacts_from_xero`` does in production
    (bulk_create sends no signals). It also used to be a necessary workaround:
    ``XeroContacts.save()`` fires ``request_glossary_refresh_on_contact``
    (apps/xero/xero_metadata/signals.py), which wrote the varchar tenant pk into
    ``ai_agent.GlossaryRefreshRequest.organisation_id``, an ``IntegerField``, and
    raised ``ValueError``. That is fixed — the receiver now writes
    ``GlossaryRefreshRequest.tenant_id`` — so a plain ``create()`` would work too.
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
# The three write actions (POST /items/, PATCH|PUT /items/<code>/, POST /items/<code>/prices/)
# require HasServiceToken as of 2026-08-19. The write-path classes below are about the write
# LOGIC — validation, effective-dating, 400-not-500 — so they authenticate in setUp and say
# nothing about the gate itself. The gate is section 9's subject, and section 9 deliberately
# does NOT inherit this credential. Distinct value from section 9's SERVICE_TOKEN so neither
# can mask a bug in the other.
WRITE_TEST_TOKEN = 'pricelist-write-path-test-token-3b7Ac1'


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


class AuthedApiTestBase(ApiTestBase):
    """Behaviour tests run as a logged-in user: since the 2026-08-20 lockdown
    (SECURITY-NOTE.md) every pricelist endpoint, reads included, requires an
    authenticated caller. The anonymous-401 contract is pinned in
    apps/user/test_auth_lockdown.py and in ServiceTokenReadRegressionTests."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user(
            username='pricelist-reader', email='pr@example.com', password='pw-not-logged')
        self.client.force_authenticate(self.user)


class ItemsListApiTests(AuthedApiTestBase):
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


@override_settings(KLIKK_API_TOKEN=WRITE_TEST_TOKEN)
class ItemDetailAndPriceApiTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {WRITE_TEST_TOKEN}')

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


@override_settings(KLIKK_API_TOKEN=WRITE_TEST_TOKEN)
class ItemsCreateApiTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {WRITE_TEST_TOKEN}')

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


@override_settings(KLIKK_API_TOKEN=WRITE_TEST_TOKEN)
class ItemPricesApiTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {WRITE_TEST_TOKEN}')

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


class QuoteApiTests(AuthedApiTestBase):
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


class ExportApiTests(AuthedApiTestBase):
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


# =========================================================================== #
# 9. Shared-secret service-token auth on the three write endpoints
#
# Adversarial by construction: EVERY denial asserts the HTTP status AND the
# database side effect. "403 but the row was still written" is the exact bug
# these tests exist to catch, so a denial test that only looks at the status
# code is not written here at all.
#
# Imports are local to this section (rather than added to the module header) so
# the 133 tests above are not restructured. ``klikk_business_intelligence.permissions``
# is imported INSIDE test bodies on purpose: if that module has not landed yet,
# only these tests fail — an ImportError at module scope would take the whole
# file down with it.
# =========================================================================== #
from django.contrib.auth import get_user_model
from django.test import override_settings

SERVICE_TOKEN = 'klikk-service-token-6d0f4c1eF9'


class ServiceTokenTestBase(ApiTestBase):
    """Fixtures + the two assertions every denial test in this section must make.

    Inherits ApiTestBase's rate card: DB-V10P (open LIST @850.00 from 2025-12-03
    plus an open TRADE @680.00 for Aurras), DB-D40, OLD-THING.
    """

    def _hdr(self, value):
        """Raw Authorization header. Kept as HTTP_* kwargs (not ``headers=``) so the
        test reads the same on any Django 5.x point release."""
        return {'HTTP_AUTHORIZATION': value}

    def _bearer(self, token=SERVICE_TOKEN):
        return self._hdr(f'Bearer {token}')

    def _new_payload(self, code='SVC-INTRUDER', **kw):
        body = {'code': code, 'name': 'created by an unauthenticated caller',
                'category': 'PA', 'unit': 'DAY', 'qty_owned': 3}
        body.update(kw)
        return body

    def _list_row(self):
        return PriceListPrice.objects.get(item=self.top, price_type='LIST', customer__isnull=True)

    def _snapshot(self):
        """Every column any of the three write endpoints could touch."""
        self.top.refresh_from_db()
        row = self._list_row()
        return {
            'items': PriceListItem.objects.count(),
            'prices': PriceListPrice.objects.count(),
            'top_name': self.top.name,
            'top_qty_owned': self.top.qty_owned,
            'top_active': self.top.active,
            'top_notes': self.top.notes,
            'top_price_rows': self.top.prices.count(),
            'open_list_price': row.price,
            'open_list_valid_from': row.valid_from,
            'open_list_valid_to': row.valid_to,
        }

    def assertDenied(self, response, label):
        self.assertIn(
            response.status_code, (401, 403),
            f'{label}: expected 401/403, got {response.status_code} {response.content[:300]!r}',
        )

    def assertNoWrite(self, before, label):
        self.assertEqual(
            self._snapshot(), before,
            f'{label}: the request was DENIED but the database still changed — the permission '
            f'check is running after the write, not before it',
        )


# --------------------------------------------------------------------------- #
# 9a. Denial — token configured, caller has no / the wrong credential
# --------------------------------------------------------------------------- #
@override_settings(KLIKK_API_TOKEN=SERVICE_TOKEN)
class ServiceTokenDenialTests(ServiceTokenTestBase):
    # ---- POST /items/ ----
    def test_post_items_no_header_is_denied_and_creates_nothing(self):
        """(1) No Authorization at all → denied AND the item row must not exist."""
        before = self._snapshot()
        r = self.client.post(self.items_url, self._new_payload(), format='json')
        self.assertDenied(r, 'POST /items/ with no Authorization header')
        self.assertFalse(
            PriceListItem.objects.filter(code='SVC-INTRUDER').exists(),
            'POST /items/ was denied but the PriceListItem row was created anyway',
        )
        self.assertNoWrite(before, 'POST /items/ with no Authorization header')

    def test_post_items_wrong_token_is_denied_and_creates_nothing(self):
        """(2) Bearer wrong-token → denied AND no row."""
        before = self._snapshot()
        r = self.client.post(self.items_url, self._new_payload(), format='json',
                             **self._bearer('wrong-token'))
        self.assertDenied(r, 'POST /items/ with a wrong bearer token')
        self.assertFalse(PriceListItem.objects.filter(code='SVC-INTRUDER').exists())
        self.assertNoWrite(before, 'POST /items/ with a wrong bearer token')

    def test_post_items_token_prefix_is_not_accepted(self):
        """A token that merely STARTS WITH / EXTENDS the real one must be rejected —
        guards against ``startswith`` / truncated comparisons."""
        before = self._snapshot()
        near_misses = (SERVICE_TOKEN[:-1], SERVICE_TOKEN[1:], SERVICE_TOKEN + 'x',
                       SERVICE_TOKEN.upper(), SERVICE_TOKEN.replace('-', '_'),
                       SERVICE_TOKEN[:len(SERVICE_TOKEN) // 2])
        for n, bad in enumerate(near_misses):
            with self.subTest(token=bad):
                r = self.client.post(self.items_url, self._new_payload(code=f'SVC-NEAR{n}'),
                                     format='json', **self._bearer(bad))
                self.assertDenied(r, f'POST /items/ with near-miss token {bad!r}')
        self.assertNoWrite(before, 'near-miss tokens')

    def test_extra_whitespace_between_scheme_and_token_is_tolerated(self):
        """DOCUMENTED LENIENCY, verified against the implementation rather than wished for.

        ``get_authorization_header(request).split()`` splits on runs of whitespace, so
        'Bearer<SP><SP><tok>' yields exactly two parts and authenticates. I originally
        asserted this as a near-miss denial; it is not one. RFC 7235 permits only a single
        SP, so this is more permissive than the spec, but the credential itself is compared
        byte-for-byte and constant-time — nothing is truncated or coerced, so it is
        tolerance, not a weakness. Pinned so a future switch to split(' ', 1) is a visible
        behaviour change.
        """
        r = self.client.post(self.items_url, self._new_payload(code='SVC-WS'),
                             format='json', **self._hdr(f'Bearer  {SERVICE_TOKEN}'))
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(PriceListItem.objects.filter(code='SVC-WS').exists())

    def test_post_items_replace_true_without_token_cannot_overwrite(self):
        """The 409/replace branch is a WRITE too — it must be behind the same gate.
        A denied replace must leave the existing item byte-for-byte unchanged."""
        before = self._snapshot()
        r = self.client.post(self.items_url,
                             {'code': 'DB-V10P', 'name': 'HIJACKED', 'category': 'PA',
                              'replace': True, 'qty_owned': 99},
                             format='json')
        self.assertDenied(r, 'POST /items/ replace=true with no token')
        self.top.refresh_from_db()
        self.assertEqual(self.top.name, 'd&b V10P point-source top')
        self.assertEqual(self.top.qty_owned, 0)
        self.assertNoWrite(before, 'POST /items/ replace=true with no token')

    # ---- PATCH / PUT /items/<code>/ ----
    def test_patch_item_no_header_is_denied_and_field_unchanged(self):
        """(3) PATCH with no header → denied AND the field on a fresh DB read is unchanged."""
        before = self._snapshot()
        r = self.client.patch(self._url('item_detail', 'DB-V10P'),
                              {'name': 'HIJACKED', 'qty_owned': 99, 'notes': 'pwned'}, format='json')
        self.assertDenied(r, 'PATCH /items/DB-V10P/ with no Authorization header')
        fresh = PriceListItem.objects.get(code='DB-V10P')
        self.assertEqual(fresh.name, 'd&b V10P point-source top',
                         'PATCH was denied but the name was written anyway')
        self.assertEqual(fresh.qty_owned, 0)
        self.assertEqual(fresh.notes, '')
        self.assertNoWrite(before, 'PATCH /items/DB-V10P/ with no Authorization header')

    def test_put_item_no_header_is_denied_and_field_unchanged(self):
        """PUT is an alias for PATCH in this view — it must be gated identically."""
        before = self._snapshot()
        r = self.client.put(self._url('item_detail', 'DB-V10P'),
                            {'name': 'HIJACKED-PUT', 'active': False}, format='json')
        self.assertDenied(r, 'PUT /items/DB-V10P/ with no Authorization header')
        fresh = PriceListItem.objects.get(code='DB-V10P')
        self.assertEqual(fresh.name, 'd&b V10P point-source top')
        self.assertTrue(fresh.active)
        self.assertNoWrite(before, 'PUT /items/DB-V10P/ with no Authorization header')

    def test_patch_item_wrong_token_is_denied_and_field_unchanged(self):
        before = self._snapshot()
        r = self.client.patch(self._url('item_detail', 'DB-V10P'), {'name': 'HIJACKED'},
                              format='json', **self._bearer('wrong-token'))
        self.assertDenied(r, 'PATCH with a wrong bearer token')
        self.assertEqual(PriceListItem.objects.get(code='DB-V10P').name, 'd&b V10P point-source top')
        self.assertNoWrite(before, 'PATCH with a wrong bearer token')

    # ---- POST /items/<code>/prices/ ----
    def test_post_price_no_header_is_denied_and_no_row_and_previous_still_open(self):
        """(4) The close-previous side effect is the dangerous one: even if the INSERT is
        rolled back, an UPDATE that stamped valid_to on the live row would silently leave
        the item with NO current price. Assert both halves."""
        before = self._snapshot()
        r = self.client.post(self._url('item_prices', 'DB-V10P'),
                             {'price': '9999.00', 'valid_from': '2026-03-01', 'set_by': 'intruder'},
                             format='json')
        self.assertDenied(r, 'POST /items/DB-V10P/prices/ with no Authorization header')
        self.top.refresh_from_db()
        self.assertEqual(self.top.prices.count(), 2, 'a price row was inserted despite the denial')
        self.assertIsNone(self._list_row().valid_to,
                          'the previously-open LIST row was CLOSED by a request that was denied — '
                          'the close-previous side effect fired before the permission check')
        self.assertEqual(self._list_row().price, Decimal('850.00'))
        self.assertNoWrite(before, 'POST prices with no Authorization header')

    def test_post_price_wrong_token_is_denied_and_previous_still_open(self):
        before = self._snapshot()
        r = self.client.post(self._url('item_prices', 'DB-V10P'),
                             {'price': '9999.00', 'valid_from': '2026-03-01'},
                             format='json', **self._bearer('wrong-token'))
        self.assertDenied(r, 'POST prices with a wrong bearer token')
        self.assertIsNone(self._list_row().valid_to)
        self.assertNoWrite(before, 'POST prices with a wrong bearer token')

    def test_post_price_same_valid_from_without_token_cannot_update_in_place(self):
        """add_price's idempotent same-day branch UPDATEs an existing row. Without a token
        that update must never happen — assert the price value itself."""
        before = self._snapshot()
        r = self.client.post(self._url('item_prices', 'DB-V10P'),
                             {'price': '1.00', 'valid_from': '2025-12-03'}, format='json')
        self.assertDenied(r, 'POST prices same-day update with no token')
        self.assertEqual(self._list_row().price, Decimal('850.00'),
                         'a denied request still rewrote the existing price row in place')
        self.assertNoWrite(before, 'POST prices same-day update with no token')

    # ---- malformed / hostile Authorization headers ----
    def test_jwt_shaped_garbage_token_is_denied_not_500(self):
        """(5) 'a.b.c' looks like a JWT to simplejwt. It must be a clean denial, never a 500."""
        before = self._snapshot()
        for bad in ('a.b.c', 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.notarealsignature', '...', 'a.b.c.d'):
            with self.subTest(token=bad):
                r = self.client.post(self.items_url, self._new_payload(code='SVC-JWTISH'),
                                     format='json', **self._bearer(bad))
                self.assertNotEqual(r.status_code, 500,
                                    f'{bad!r} produced a 500: {r.content[:300]!r}')
                self.assertDenied(r, f'POST /items/ with JWT-shaped garbage {bad!r}')
        self.assertFalse(PriceListItem.objects.filter(code='SVC-JWTISH').exists())
        self.assertNoWrite(before, 'JWT-shaped garbage tokens')

    def test_wrong_scheme_token_is_denied(self):
        """(6a) 'Token <tok>' — the shared secret must only be honoured on the Bearer scheme.
        (DRF's TokenAuthentication is also registered, so this lands on the authtoken table
        and 401s; either way it must not write.)"""
        before = self._snapshot()
        r = self.client.post(self.items_url, self._new_payload(code='SVC-SCHEME'),
                             format='json', **self._hdr(f'Token {SERVICE_TOKEN}'))
        self.assertDenied(r, "POST /items/ with 'Token <tok>' scheme")
        self.assertFalse(PriceListItem.objects.filter(code='SVC-SCHEME').exists())
        self.assertNoWrite(before, "'Token <tok>' scheme")

    def test_bearer_with_no_value_is_denied(self):
        """(6b) 'Bearer' and 'Bearer ' carry no credential. If the implementation splits on
        ' ' with maxsplit and gets '', a mis-guarded compare against a configured token is
        still a denial — but this is the shape that becomes a bypass the moment the setting
        is empty (see ServiceTokenUnsetTests)."""
        before = self._snapshot()
        for header in ('Bearer', 'Bearer ', 'Bearer  ', 'bearer'):
            with self.subTest(header=header):
                r = self.client.post(self.items_url, self._new_payload(code='SVC-NOVALUE'),
                                     format='json', **self._hdr(header))
                self.assertNotEqual(r.status_code, 500, f'{header!r} produced a 500')
                self.assertDenied(r, f'POST /items/ with {header!r}')
        self.assertFalse(PriceListItem.objects.filter(code='SVC-NOVALUE').exists())
        self.assertNoWrite(before, 'Bearer with no value')

    def test_lowercase_bearer_scheme_is_ACCEPTED_per_contract(self):
        """(6c) The contract says ServiceTokenAuthentication lower-cases the scheme, so
        'bearer <tok>' must be ACCEPTED.

        FLAGGED, because it surprised me: this is looser than every other authenticator in
        the stack. simplejwt compares the scheme byte-for-byte against AUTH_HEADER_TYPES
        ('Bearer'), so 'bearer <jwt>' is NOT accepted for a console JWT — a caller can use a
        lowercase scheme for the shared secret but not for a JWT. RFC 7235 does say the
        scheme is case-insensitive, so accepting it is defensible; the inconsistency with
        JWTAuthentication is the thing worth knowing about, not a vulnerability.
        """
        r = self.client.post(self.items_url, self._new_payload(code='SVC-LOWER'),
                             format='json', **self._hdr(f'bearer {SERVICE_TOKEN}'))
        self.assertEqual(r.status_code, 201,
                         f'contract says the scheme is lower-cased, so "bearer <tok>" must be '
                         f'accepted; got {r.status_code} {r.content[:300]!r}')
        self.assertTrue(PriceListItem.objects.filter(code='SVC-LOWER').exists())

    def test_extra_parts_in_authorization_header_are_denied(self):
        """'Bearer <tok> extra' is malformed — must not authenticate, must not 500."""
        before = self._snapshot()
        r = self.client.post(self.items_url, self._new_payload(code='SVC-EXTRA'),
                             format='json', **self._hdr(f'Bearer {SERVICE_TOKEN} extra'))
        self.assertNotEqual(r.status_code, 500)
        self.assertDenied(r, 'Bearer <tok> extra')
        self.assertNoWrite(before, 'Bearer <tok> extra')

    def test_non_ascii_token_does_not_500(self):
        """``hmac.compare_digest`` raises TypeError when handed a str with non-ASCII
        characters, and ``bytes.decode()`` (utf-8) raises UnicodeDecodeError on a latin-1
        header byte. Either turns a hostile header into a 500. Must be a clean denial."""
        before = self._snapshot()
        for bad in ('wr\xf6ng-token', '\xe9' * 40, SERVICE_TOKEN + '\xff'):
            with self.subTest(token=bad):
                r = self.client.post(self.items_url, self._new_payload(code='SVC-NONASCII'),
                                     format='json', **self._bearer(bad))
                self.assertNotEqual(
                    r.status_code, 500,
                    f'non-ASCII bearer token {bad!r} produced a 500 — the comparison is not '
                    f'byte-safe (hmac.compare_digest on non-ASCII str, or a utf-8 .decode())',
                )
                self.assertDenied(r, f'non-ASCII token {bad!r}')
        self.assertNoWrite(before, 'non-ASCII tokens')

    def test_very_long_token_is_denied_not_500(self):
        before = self._snapshot()
        r = self.client.post(self.items_url, self._new_payload(code='SVC-LONG'),
                             format='json', **self._bearer('x' * 8000))
        self.assertNotEqual(r.status_code, 500)
        self.assertDenied(r, 'an 8000-char bearer token')
        self.assertNoWrite(before, 'an 8000-char bearer token')

    def test_denial_response_body_never_echoes_the_configured_token(self):
        """A 401/403 body that quotes the expected secret would be a disclosure bug."""
        r = self.client.post(self.items_url, self._new_payload(code='SVC-ECHO'),
                             format='json', **self._bearer('wrong-token'))
        self.assertNotIn(SERVICE_TOKEN, r.content.decode('utf-8', 'replace'))


# --------------------------------------------------------------------------- #
# 9b. Denial — KLIKK_API_TOKEN unset (the empty-secret bypass)
# --------------------------------------------------------------------------- #
@override_settings(KLIKK_API_TOKEN='')
class ServiceTokenUnsetTests(ServiceTokenTestBase):
    """With no token configured the endpoints must be closed to anonymous callers, not open.

    The hunt here is ``if token == settings.KLIKK_API_TOKEN`` without a non-empty guard:
    an empty setting plus an empty/absent credential compares equal and authenticates
    *everyone*. This is the single worst failure mode of the whole feature, because the
    setting is empty by default — a deploy that forgets the env var would ship the API wide
    open while every happy-path test still passes.
    """

    def test_empty_setting_plus_any_bearer_is_denied_and_writes_nothing(self):
        """(7) KLIKK_API_TOKEN='' + 'Bearer anything' → denied, no write, no 500."""
        before = self._snapshot()
        for tok in ('anything', '', ' ', 'None', SERVICE_TOKEN):
            with self.subTest(token=tok):
                r = self.client.post(self.items_url, self._new_payload(code='SVC-EMPTY'),
                                     format='json', **self._bearer(tok))
                self.assertNotEqual(r.status_code, 500, f'Bearer {tok!r} produced a 500')
                self.assertDenied(r, f'KLIKK_API_TOKEN="" + Bearer {tok!r}')
        self.assertFalse(
            PriceListItem.objects.filter(code='SVC-EMPTY').exists(),
            'EMPTY-SECRET BYPASS: with KLIKK_API_TOKEN unset a bearer credential still '
            'authenticated and the write went through',
        )
        self.assertNoWrite(before, 'KLIKK_API_TOKEN="" with a bearer credential')

    def test_empty_setting_plus_bare_bearer_scheme_is_denied(self):
        """The exact '' == '' shape: empty setting AND an empty credential."""
        before = self._snapshot()
        for header in ('Bearer', 'Bearer ', 'bearer ', 'Bearer  '):
            with self.subTest(header=header):
                r = self.client.post(self.items_url, self._new_payload(code='SVC-EMPTY2'),
                                     format='json', **self._hdr(header))
                self.assertNotEqual(r.status_code, 500)
                self.assertDenied(r, f'KLIKK_API_TOKEN="" + {header!r}')
        self.assertFalse(PriceListItem.objects.filter(code='SVC-EMPTY2').exists())
        self.assertNoWrite(before, 'KLIKK_API_TOKEN="" + a bare Bearer scheme')

    def test_empty_setting_no_header_denied_on_all_three_writes(self):
        """(7b) KLIKK_API_TOKEN='' + no header → all three writes denied, nothing written."""
        before = self._snapshot()

        r = self.client.post(self.items_url, self._new_payload(code='SVC-EMPTY3'), format='json')
        self.assertNotEqual(r.status_code, 500)
        self.assertDenied(r, 'POST /items/ (token unset, no header)')
        self.assertFalse(PriceListItem.objects.filter(code='SVC-EMPTY3').exists())

        r = self.client.patch(self._url('item_detail', 'DB-V10P'), {'name': 'HIJACKED'}, format='json')
        self.assertNotEqual(r.status_code, 500)
        self.assertDenied(r, 'PATCH /items/DB-V10P/ (token unset, no header)')
        self.assertEqual(PriceListItem.objects.get(code='DB-V10P').name, 'd&b V10P point-source top')

        r = self.client.post(self._url('item_prices', 'DB-V10P'),
                             {'price': '9999.00', 'valid_from': '2026-03-01'}, format='json')
        self.assertNotEqual(r.status_code, 500)
        self.assertDenied(r, 'POST prices (token unset, no header)')
        self.assertIsNone(self._list_row().valid_to)

        self.assertNoWrite(before, 'KLIKK_API_TOKEN="" with no Authorization header')

    def test_missing_setting_entirely_is_denied_not_500(self):
        """ROBUSTNESS, BEYOND THE LITERAL CONTRACT (the contract guarantees base.py defines
        KLIKK_API_TOKEN). The realistic scenario is a deploy where permissions.py lands
        before settings/base.py, or a settings module that does not import base. Reading the
        setting with ``getattr(settings, 'KLIKK_API_TOKEN', '')`` costs nothing and turns a
        500 into a denial. Failing this is advisory, not a contract breach."""
        before = self._snapshot()
        with override_settings():
            from django.conf import settings as dj_settings
            if hasattr(dj_settings, 'KLIKK_API_TOKEN'):
                del dj_settings.KLIKK_API_TOKEN
            r = self.client.post(self.items_url, self._new_payload(code='SVC-NOSETTING'), format='json')
            self.assertNotEqual(
                r.status_code, 500,
                f'KLIKK_API_TOKEN absent from settings produced a 500 — read it with '
                f'getattr(settings, "KLIKK_API_TOKEN", ""): {r.content[:300]!r}',
            )
            self.assertDenied(r, 'KLIKK_API_TOKEN absent from settings')
        self.assertFalse(PriceListItem.objects.filter(code='SVC-NOSETTING').exists())
        self.assertNoWrite(before, 'KLIKK_API_TOKEN absent from settings')


# --------------------------------------------------------------------------- #
# 9c. Success — the correct token, on all three write actions
# --------------------------------------------------------------------------- #
@override_settings(KLIKK_API_TOKEN=SERVICE_TOKEN)
class ServiceTokenSuccessTests(ServiceTokenTestBase):
    def test_post_items_with_token_creates_the_row(self):
        """(8) 201 AND the row is in the DB with the values that were sent.

        Also the ordering canary: if ServiceTokenAuthentication is not FIRST in
        DEFAULT_AUTHENTICATION_CLASSES, JWTAuthentication sees the Bearer header first,
        fails to decode the shared secret as a JWT and raises 401 before our authenticator
        ever runs. A 401 here means the registration order is wrong.
        """
        r = self.client.post(self.items_url,
                             {'code': 'svc-cdj3000 ', 'name': 'Pioneer CDJ-3000', 'category': 'dj',
                              'unit': 'day', 'qty_owned': 4, 'notes': 'via service token'},
                             format='json', **self._bearer())
        self.assertEqual(r.status_code, 201, r.content)
        row = PriceListItem.objects.get(code='SVC-CDJ3000')
        self.assertEqual(row.name, 'Pioneer CDJ-3000')
        self.assertEqual(row.category, 'DJ')
        self.assertEqual(row.unit, 'DAY')
        self.assertEqual(row.qty_owned, 4)
        self.assertEqual(row.notes, 'via service token')
        self.assertTrue(r.json()['created'])

    def test_post_items_with_token_and_opening_price_writes_both_rows(self):
        """The atomic item+opening-price branch must work through the gate too."""
        r = self.client.post(self.items_url,
                             {'code': 'SVC-OPEN', 'name': 'With opening price', 'category': 'PA',
                              'price': '123.45', 'valid_from': '2026-01-01'},
                             format='json', **self._bearer())
        self.assertEqual(r.status_code, 201, r.content)
        item = PriceListItem.objects.get(code='SVC-OPEN')
        row = item.prices.get()
        self.assertEqual(row.price, Decimal('123.45'))
        self.assertEqual(row.valid_from, D(2026, 1, 1))
        self.assertIsNone(row.valid_to)

    def test_post_items_replace_true_with_token_updates(self):
        """(9) replace=true + token → 200 and the DB row is updated."""
        r = self.client.post(self.items_url,
                             {'code': 'DB-V10P', 'name': 'd&b V10P (renamed by service)',
                              'category': 'PA', 'replace': True, 'qty_owned': 7},
                             format='json', **self._bearer())
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()['created'])
        self.top.refresh_from_db()
        self.assertEqual(self.top.name, 'd&b V10P (renamed by service)')
        self.assertEqual(self.top.qty_owned, 7)
        # and the price history is untouched by a replace
        self.assertEqual(self.top.prices.count(), 2)

    def test_patch_item_with_token_changes_the_db_row(self):
        """(10) PATCH + token → 200 and the DB row actually changed."""
        r = self.client.patch(self._url('item_detail', 'DB-V10P'),
                              {'name': 'patched by service', 'qty_owned': 11, 'notes': 'ok'},
                              format='json', **self._bearer())
        self.assertEqual(r.status_code, 200, r.content)
        fresh = PriceListItem.objects.get(code='DB-V10P')
        self.assertEqual(fresh.name, 'patched by service')
        self.assertEqual(fresh.qty_owned, 11)
        self.assertEqual(fresh.notes, 'ok')

    def test_put_item_with_token_changes_the_db_row(self):
        r = self.client.put(self._url('item_detail', 'DB-V10P'), {'name': 'put by service'},
                            format='json', **self._bearer())
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(PriceListItem.objects.get(code='DB-V10P').name, 'put by service')

    def test_post_price_with_token_creates_row_and_closes_previous(self):
        """(11) 201, the new row exists open, AND the previous open row's valid_to is the
        day BEFORE valid_from — i.e. the effective-dating side effect really ran."""
        r = self.client.post(self._url('item_prices', 'DB-V10P'),
                             {'price': '900', 'valid_from': '2026-03-01', 'set_by': 'svc',
                              'note': 'rate rise'},
                             format='json', **self._bearer())
        self.assertEqual(r.status_code, 201, r.content)
        self.top.refresh_from_db()
        self.assertEqual(self.top.prices.count(), 3)
        new = PriceListPrice.objects.get(item=self.top, price_type='LIST', valid_from=D(2026, 3, 1))
        self.assertEqual(new.price, Decimal('900.00'))
        self.assertIsNone(new.valid_to)
        self.assertEqual(new.set_by, 'svc')
        closed = PriceListPrice.objects.get(item=self.top, price_type='LIST', valid_from=D(2025, 12, 3))
        self.assertEqual(closed.valid_to, D(2026, 3, 1) - ONE_DAY)
        self.assertEqual(closed.valid_to, D(2026, 2, 28))
        self.assertEqual(r.json()['closed_previous']['valid_to'], '2026-02-28')

    def test_token_write_does_not_persist_a_user(self):
        """ServiceAccount is a stand-in, not a row. A service write must not create a User."""
        UserModel = get_user_model()
        before = UserModel.objects.count()
        r = self.client.post(self.items_url, self._new_payload(code='SVC-NOUSER'),
                             format='json', **self._bearer())
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(UserModel.objects.count(), before,
                         'the service account was persisted as a real User row')

    def test_success_response_never_echoes_the_token(self):
        r = self.client.post(self.items_url, self._new_payload(code='SVC-NOECHO'),
                             format='json', **self._bearer())
        self.assertEqual(r.status_code, 201, r.content)
        self.assertNotIn(SERVICE_TOKEN, r.content.decode('utf-8', 'replace'))

    def test_reads_still_work_while_holding_a_valid_token(self):
        """A valid service token must not break the read endpoints (e.g. by producing a
        ServiceAccount that some downstream code cannot handle)."""
        self.assertEqual(self.client.get(self.items_url, **self._bearer()).status_code, 200)
        self.assertEqual(self.client.get(self._url('item_detail', 'DB-V10P'), **self._bearer()).status_code, 200)
        self.assertEqual(self.client.get(self._url('export'), **self._bearer()).status_code, 200)
        r = self.client.post(self._url('quote'), {'lines': [{'code': 'DB-V10P', 'qty': 1, 'days': 1}]},
                             format='json', **self._bearer())
        self.assertEqual(r.status_code, 200, r.content)


# --------------------------------------------------------------------------- #
# 9d. No regression on reads or on /quote/
# --------------------------------------------------------------------------- #
class ServiceTokenReadRegressionTests(ServiceTokenTestBase):
    READ_CASES = (
        ('items', None),
        ('item_detail', 'DB-V10P'),
        ('item_price', 'DB-V10P'),
        ('item_prices', 'DB-V10P'),
        ('export', None),
    )

    def test_all_reads_are_closed_to_anonymous_callers_token_set_and_unset(self):
        """(12, INVERTED 2026-08-20) Every GET must be 401 with NO Authorization header,
        whether or not KLIKK_API_TOKEN is configured. This is the opposite of the original
        contract: SECURITY-NOTE.md's lockdown made IsAuthenticated the project default and
        HasServiceToken now gates reads too. A 200 here means the general-ledger-era
        anonymous surface is creeping back."""
        for token_setting in (SERVICE_TOKEN, ''):
            for name, code in self.READ_CASES:
                with self.subTest(KLIKK_API_TOKEN=token_setting or '<unset>', endpoint=name):
                    with override_settings(KLIKK_API_TOKEN=token_setting):
                        r = self.client.get(self._url(name, code))
                    self.assertEqual(r.status_code, 401,
                                     f'GET {name} answered {r.status_code} anonymously: '
                                     f'{r.content[:200]!r}')

    def test_quote_closed_anonymously_but_works_with_service_token(self):
        """(13, INVERTED 2026-08-20) POST /quote/ persists nothing, but it prices the rate
        card, so it is data disclosure all the same. Anonymous → 401; the service token
        (or a console JWT) still gets the calculation."""
        body = {'lines': [{'code': 'DB-V10P', 'qty': 4, 'days': 3},
                          {'code': 'DB-D40', 'qty': 1, 'days': 3}],
                'date': '2026-02-01'}
        for token_setting in (SERVICE_TOKEN, ''):
            with self.subTest(KLIKK_API_TOKEN=token_setting or '<unset>'):
                with override_settings(KLIKK_API_TOKEN=token_setting):
                    r = self.client.post(self._url('quote'), body, format='json')
                self.assertEqual(r.status_code, 401,
                                 f'anonymous POST /quote/ answered {r.status_code}: '
                                 f'{r.content[:300]!r}')
        with override_settings(KLIKK_API_TOKEN=SERVICE_TOKEN):
            r = self.client.post(self._url('quote'), body, format='json', **self._bearer())
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['lines'][0]['unit_price'], '850.00')
        self.assertEqual(PriceListPrice.objects.count(), 4, '/quote/ must persist nothing')

    def test_quote_400_path_is_still_a_400_not_an_auth_error(self):
        """A bad /quote/ body from an AUTHENTICATED caller must still be the app's 400 —
        proof the request reached the view rather than being stopped by a permission
        class."""
        with override_settings(KLIKK_API_TOKEN=SERVICE_TOKEN):
            r = self.client.post(self._url('quote'), {'lines': []}, format='json',
                                 **self._bearer())
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('lines', r.json()['detail'])

    @override_settings(KLIKK_API_TOKEN=SERVICE_TOKEN)
    def test_get_with_a_garbage_bearer_header_documented_behaviour(self):
        """(14) A junk Bearer value 401s from JWTAuthentication (raise = stop the chain,
        DRF's documented authenticator contract). Since the 2026-08-20 lockdown a GET with
        no header at all ALSO 401s — from the permission layer instead. Both paths closed,
        different layers; pinned so a change in either is noticed."""
        r = self.client.get(self.items_url, **self._bearer('garbage'))
        self.assertEqual(
            r.status_code, 401,
            f'expected the documented 401 from JWTAuthentication on a junk Bearer header; '
            f'got {r.status_code} {r.content[:300]!r}',
        )
        self.assertEqual(self.client.get(self.items_url).status_code, 401)


# --------------------------------------------------------------------------- #
# 9e. The console (a real logged-in Django user) must still be able to write
# --------------------------------------------------------------------------- #
@override_settings(KLIKK_API_TOKEN='')
class ConsoleUserWriteTests(ServiceTokenTestBase):
    """(15) KLIKK_API_TOKEN deliberately UNSET: the console must not depend on the shared
    secret existing. force_authenticate bypasses the authenticators and sets request.user,
    which is exactly the console/session path HasServiceToken's second clause covers."""

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(
            username='console-mc', email='mc@example.test', password='pw-not-used-by-force-auth')
        self.client.force_authenticate(user=self.user)

    def test_console_user_can_post_items(self):
        r = self.client.post(self.items_url,
                             {'code': 'CON-1', 'name': 'Console item', 'category': 'PA'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(PriceListItem.objects.filter(code='CON-1').exists())

    def test_console_user_can_patch_item(self):
        r = self.client.patch(self._url('item_detail', 'DB-V10P'),
                              {'name': 'console rename', 'qty_owned': 2}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        fresh = PriceListItem.objects.get(code='DB-V10P')
        self.assertEqual(fresh.name, 'console rename')
        self.assertEqual(fresh.qty_owned, 2)

    def test_console_user_can_post_price_and_close_previous(self):
        r = self.client.post(self._url('item_prices', 'DB-V10P'),
                             {'price': '910', 'valid_from': '2026-04-01'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(
            PriceListPrice.objects.get(item=self.top, price_type='LIST', valid_from=D(2025, 12, 3)).valid_to,
            D(2026, 3, 31))
        self.assertIsNone(
            PriceListPrice.objects.get(item=self.top, price_type='LIST', valid_from=D(2026, 4, 1)).valid_to)

    def test_inactive_user_is_not_treated_as_authenticated(self):
        """An INACTIVE user must not be able to write. ``is_authenticated`` is True even for
        an inactive user object, so a permission that only checks that flag will let a
        disabled account keep writing. Driven through the real authenticator chain (a JWT
        for the disabled user) rather than force_authenticate, which would bypass the
        is_active check that lives in authentication.
        """
        from rest_framework_simplejwt.tokens import AccessToken
        disabled = get_user_model().objects.create_user(
            username='disabled-mc', email='x@example.test', password='pw', is_active=False)
        token = str(AccessToken.for_user(disabled))
        client = APIClient()
        client.raise_request_exception = False
        r = client.post(self.items_url, {'code': 'CON-DISABLED', 'name': 'nope', 'category': 'PA'},
                        format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertIn(r.status_code, (401, 403),
                      f'a DISABLED user wrote to the rate card: {r.status_code} {r.content[:300]!r}')
        self.assertFalse(PriceListItem.objects.filter(code='CON-DISABLED').exists())


# --------------------------------------------------------------------------- #
# 9f. Structural: the decorator is on EXACTLY the three write views
#
# Supplementary to the endpoint tests above, not a substitute for them. It exists
# because "NOT applied to item_price_view / export_view" is otherwise unobservable
# over HTTP (both are GET-only, and HasServiceToken allows SAFE_METHODS), and
# because @permission_classes silently no-ops if it is stacked ABOVE @api_view
# instead of below it.
# --------------------------------------------------------------------------- #
class ServiceTokenWiringTests(TestCase):
    GATED = ('items_view', 'item_detail_view', 'item_prices_view')
    OPEN = ('quote_view', 'export_view', 'item_price_view')

    def _permission_classes(self, view_name):
        from . import views as pricelist_views
        view = getattr(pricelist_views, view_name)
        cls = getattr(view, 'cls', None)
        self.assertIsNotNone(cls, f'{view_name} is not an @api_view-wrapped view')
        return tuple(getattr(cls, 'permission_classes', ()))

    def test_permission_class_is_on_exactly_the_three_write_views(self):
        from klikk_business_intelligence.permissions import HasServiceToken
        for name in self.GATED:
            with self.subTest(view=name):
                self.assertIn(HasServiceToken, self._permission_classes(name),
                              f'{name} is NOT gated by HasServiceToken (is @permission_classes '
                              f'stacked ABOVE @api_view? that silently does nothing)')
        for name in self.OPEN:
            with self.subTest(view=name):
                self.assertNotIn(HasServiceToken, self._permission_classes(name),
                                 f'{name} must stay open — the contract names exactly three gated views')

    def test_service_token_authentication_is_registered_first(self):
        """If it is not FIRST, JWTAuthentication raises on the shared secret before our
        authenticator is ever consulted and every token write 401s."""
        from django.conf import settings as dj_settings
        classes = dj_settings.REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']
        self.assertTrue(
            str(classes[0]).endswith('ServiceTokenAuthentication'),
            f'ServiceTokenAuthentication must be the FIRST authentication class; got {classes}',
        )

    def test_service_account_is_not_a_model_instance(self):
        from django.db import models as django_models

        from klikk_business_intelligence.permissions import ServiceAccount
        account = ServiceAccount()
        self.assertTrue(account.is_authenticated)
        self.assertFalse(isinstance(account, django_models.Model),
                         'ServiceAccount must be a non-persisted stand-in, not a Django model')
        self.assertIsNone(getattr(account, 'pk', None),
                          'ServiceAccount must not carry a primary key')

    def test_klikk_api_token_setting_exists_and_is_a_string(self):
        from django.conf import settings as dj_settings
        self.assertIsInstance(getattr(dj_settings, 'KLIKK_API_TOKEN', None), str,
                              'KLIKK_API_TOKEN must exist and default to "" when the env var is unset')

    def test_token_is_read_at_request_time_not_import_time(self):
        """override_settings must actually take effect. If the authenticator snapshots
        ``settings.KLIKK_API_TOKEN`` into a module-level constant at import, the value is
        frozen to whatever the environment held when Django booted — the setting becomes
        un-rotatable without a restart, and every override_settings test above is silently
        meaningless."""
        client = APIClient()
        client.raise_request_exception = False
        url = reverse('pricelist:items')
        with override_settings(KLIKK_API_TOKEN='first-secret'):
            r = client.post(url, {'code': 'RT-1', 'name': 'rt', 'category': 'PA'},
                            format='json', HTTP_AUTHORIZATION='Bearer first-secret')
            self.assertEqual(r.status_code, 201, f'token not read at request time: {r.content[:300]!r}')
        with override_settings(KLIKK_API_TOKEN='second-secret'):
            r = client.post(url, {'code': 'RT-2', 'name': 'rt', 'category': 'PA'},
                            format='json', HTTP_AUTHORIZATION='Bearer first-secret')
            self.assertIn(r.status_code, (401, 403),
                          'the OLD token still authenticated after the setting was rotated — '
                          'KLIKK_API_TOKEN is cached at import time')
            self.assertFalse(PriceListItem.objects.filter(code='RT-2').exists())
            r = client.post(url, {'code': 'RT-3', 'name': 'rt', 'category': 'PA'},
                            format='json', HTTP_AUTHORIZATION='Bearer second-secret')
            self.assertEqual(r.status_code, 201, f'the rotated token was not honoured: {r.content[:300]!r}')

# --------------------------------------------------------------------------- #
# 9g. The 401-vs-403 challenge shape — a PROJECT-WIDE side effect
#
# Prepending an authentication class to DEFAULT_AUTHENTICATION_CLASSES changes the
# status code of EVERY unauthenticated request in the project, not just this app's.
# DRF derives the shape from ``get_authenticators()[0].authenticate_header()`` alone
# (rest_framework/views.py: get_authenticate_header -> handle_exception) and coerces
# NotAuthenticated / AuthenticationFailed to 403 when that returns None. So an
# authentication class that omits ``authenticate_header`` silently turns every 401 in
# klikk_financials — /xero/cube/*, /api/ai-agent/*, everything IsAuthenticated — into a
# 403, breaking any client that branches on 401 to refresh its JWT.
#
# The denial tests above deliberately accept either 401 or 403, so none of them can catch
# this. These two pin it explicitly, including one endpoint OUTSIDE apps.pricelist —
# scope creep into another app's URL is intentional here: apps/pricelist/tests.py is the
# only file this change is allowed to touch, and the blast radius is not local to it.
# --------------------------------------------------------------------------- #
@override_settings(KLIKK_API_TOKEN=SERVICE_TOKEN)
class AuthChallengeShapeTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.raise_request_exception = False

    def test_unauthenticated_pricelist_write_is_401_not_403(self):
        r = self.client.post(reverse('pricelist:items'),
                             {'code': 'SHAPE-1', 'name': 'shape', 'category': 'PA'}, format='json')
        self.assertEqual(
            r.status_code, 401,
            f'expected 401 (the documented shape); got {r.status_code}. A 403 here means the '
            f'first authentication class does not implement authenticate_header()',
        )
        self.assertEqual(r['WWW-Authenticate'], 'Bearer realm="api"')
        self.assertFalse(PriceListItem.objects.filter(code='SHAPE-1').exists())

    def test_unauthenticated_view_in_another_app_still_returns_401(self):
        """Regression canary for the rest of the backend: an IsAuthenticated view that has
        nothing to do with the rate card must still answer 401, not 403."""
        r = self.client.get(reverse('xero_cube:xero-data-summary'))
        self.assertEqual(
            r.status_code, 401,
            f'/xero/cube/summary/ now answers {r.status_code} instead of 401 — prepending '
            f'ServiceTokenAuthentication changed the challenge shape for the WHOLE project',
        )
        self.assertEqual(r['WWW-Authenticate'], 'Bearer realm="api"')
