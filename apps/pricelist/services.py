"""
Pure logic for the equipment price list — resolution, effective-dating, quotes.

* ``get_price`` / ``resolve_price`` — which price applies to an item on a date
  for a customer (customer-specific lane first, then the general lane, then
  LIST as the final fallback).
* ``add_price``       — append an effective-dated row and close the previous
                         open row in the same lane (idempotent re-set of the
                         same ``valid_from`` updates in place).
* ``build_quote``     — PURE calculation: lines x qty x days, discount, VAT.
                         Persists nothing.
* ``item_to_dict`` / ``price_to_dict`` — JSON shapes used by the views.
* ``resolve_customer`` — contacts_id OR name -> XeroContacts.

Money rules (non-negotiable): ``Decimal`` everywhere, ``ROUND_HALF_UP`` at each
rounding point, and every money value serialised as a 2-dp *string* so JSON
never turns R1,234.50 into a float. ``q2`` is the single quantiser.

Nothing here calls Xero; the only Xero tables touched are the local mirror
(``xero_metadata_xerocontacts``, ``xero_data_xeroinvoicelineitem``), read-only.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.xero.xero_metadata.models import XeroContacts

from .models import PRICE_TYPE_VALUES, PriceListItem, PriceListPrice

VAT_RATE_DEFAULT = Decimal('0.15')
_CENTS = Decimal('0.01')
# PriceListPrice.price is numeric(12,2) -> 10 integer digits. Anything at or above this
# overflows the column; we reject it in Python so the caller gets a 400, not a DataError 500.
MAX_PRICE = Decimal('10') ** 10


# --------------------------------------------------------------------------- #
# Money helpers
# --------------------------------------------------------------------------- #
def q2(value) -> Decimal:
    """Quantise to cents, ROUND_HALF_UP. Accepts Decimal / int / str (never pass a float)."""
    return Decimal(value).quantize(_CENTS, rounding=ROUND_HALF_UP)


def money(value) -> str | None:
    """Serialise money for JSON: 2-dp string, or None for None."""
    return None if value is None else str(q2(value))


def to_decimal(value, *, field: str = 'value') -> Decimal:
    """Parse user input into a Decimal; raise ValueError with a clear message on junk.

    Floats are accepted but routed through str() so 0.1 does not become
    0.1000000000000000055511151231257827; callers should still prefer strings.
    """
    if isinstance(value, bool):
        # bool is an int subclass — True would otherwise silently become Decimal('1').
        raise ValueError(f'{field} must be a number')
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value).strip())
        except (InvalidOperation, ValueError, TypeError, AttributeError):
            raise ValueError(f'{field} must be a number, got {value!r}') from None
    # 'NaN' / 'Infinity' / 'sNaN' parse happily into Decimal and then explode much later —
    # inside quantize() or on a comparison (NaN ordering raises InvalidOperation, which is an
    # ArithmeticError, NOT a ValueError, so nothing upstream catches it and the view 500s).
    # Reject them here, at the single parsing choke point.
    if not parsed.is_finite():
        raise ValueError(f'{field} must be a finite number, got {value!r}')
    return parsed


# --------------------------------------------------------------------------- #
# Customer resolution
# --------------------------------------------------------------------------- #
def _customer_id(customer) -> str | None:
    if customer is None:
        return None
    if isinstance(customer, XeroContacts):
        return customer.contacts_id
    return str(customer)


def resolve_customer(value) -> XeroContacts | None:
    """Accept a contacts_id UUID string OR a name. Return None for falsy input.

    Lookup order: exact contacts_id, exact name, case-insensitive exact name,
    then ``name__icontains``. If the contains-match is ambiguous (more than one
    contact), raise ValueError listing the candidates so the caller can pick.
    """
    if not value:
        return None
    if isinstance(value, XeroContacts):
        return value
    needle = str(value).strip()
    if not needle:
        return None
    hit = XeroContacts.objects.filter(contacts_id=needle).first()
    if hit:
        return hit
    hit = XeroContacts.objects.filter(name=needle).first()
    if hit:
        return hit
    hit = XeroContacts.objects.filter(name__iexact=needle).first()
    if hit:
        return hit
    candidates = list(XeroContacts.objects.filter(name__icontains=needle).order_by('name')[:11])
    if not candidates:
        return None
    if len(candidates) > 1:
        names = ', '.join(f'{c.name} ({c.contacts_id})' for c in candidates[:10])
        more = ' …' if len(candidates) > 10 else ''
        raise ValueError(f'customer {needle!r} is ambiguous — matches: {names}{more}')
    return candidates[0]


def customer_to_dict(customer: XeroContacts | None) -> dict | None:
    if customer is None:
        return None
    return {'contacts_id': customer.contacts_id, 'name': customer.name}


# --------------------------------------------------------------------------- #
# Price resolution
# --------------------------------------------------------------------------- #
def _valid_on(qs, on_date: dt.date):
    return qs.filter(valid_from__lte=on_date).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on_date))


def get_price(item: PriceListItem, on_date: dt.date | None = None, customer=None,
              price_type: str = 'LIST') -> PriceListPrice | None:
    """Resolution order — customer-specific first, then the open general row.

    1. rows valid on ``on_date`` (valid_from <= on_date AND (valid_to IS NULL OR valid_to >= on_date))
    2. if ``customer`` given: rows with customer=customer, newest first (-valid_from, -id) -> first match
       (any price_type — a customer row is an explicit override regardless of its lane label)
    3. else/fallback: customer IS NULL AND price_type == price_type, newest first -> first match
    4. if still nothing and price_type != 'LIST': same but price_type='LIST'
    5. else None

    ``customer`` may be a XeroContacts instance OR a contacts_id string.
    """
    on_date = on_date or timezone.localdate()
    price_type = (price_type or 'LIST').upper()
    base = _valid_on(PriceListPrice.objects.filter(item=item), on_date).select_related('customer')
    newest = ('-valid_from', '-id')

    cid = _customer_id(customer)
    if cid:
        row = base.filter(customer_id=cid).order_by(*newest).first()
        if row:
            return row

    row = base.filter(customer__isnull=True, price_type=price_type).order_by(*newest).first()
    if row:
        return row
    if price_type != 'LIST':
        return base.filter(customer__isnull=True, price_type='LIST').order_by(*newest).first()
    return None


def resolve_price(item: PriceListItem, on_date: dt.date | None = None, customer=None,
                  price_type: str = 'LIST') -> dict:
    """``get_price`` wrapped in a JSON-ready dict that says *why* this price was chosen."""
    on_date = on_date or timezone.localdate()
    price_type = (price_type or 'LIST').upper()
    row = get_price(item, on_date=on_date, customer=customer, price_type=price_type)
    requested_cid = _customer_id(customer)
    out = {
        'code': item.code,
        'name': item.name,
        'unit': item.unit,
        'date': on_date.isoformat(),
        'requested_price_type': price_type,
        'price': None,
        'price_type': None,
        'customer_id': requested_cid,
        'customer_name': getattr(customer, 'name', None) if isinstance(customer, XeroContacts) else None,
        'valid_from': None,
        'valid_to': None,
        'note': '',
        'set_by': '',
        'price_id': None,
        'resolved': False,
        # True when a non-LIST type was asked for but only a LIST row exists (step 4 above).
        'fallback_to_list': False,
    }
    if row is None:
        return out
    out.update({
        'price': money(row.price),
        'price_type': row.price_type,
        'valid_from': row.valid_from.isoformat(),
        'valid_to': row.valid_to.isoformat() if row.valid_to else None,
        'note': row.note,
        'set_by': row.set_by,
        'price_id': row.id,
        'resolved': True,
        'fallback_to_list': bool(price_type != 'LIST' and row.price_type == 'LIST' and row.customer_id is None),
    })
    if row.customer_id:
        out['customer_id'] = row.customer_id
        out['customer_name'] = row.customer.name if row.customer else None
    return out


# --------------------------------------------------------------------------- #
# Effective-dated writes
# --------------------------------------------------------------------------- #
def add_price(item: PriceListItem, *, price, valid_from: dt.date, price_type: str = 'LIST',
              customer=None, note: str = '', set_by: str = '') -> tuple[PriceListPrice, PriceListPrice | None]:
    """Add a new effective-dated row and close the previous open row IN THE SAME LANE.

    A lane = (item, price_type, customer). Rules:
    * a row in the lane with ``valid_from == valid_from`` -> UPDATE it in place (idempotent
      re-seed / re-set of the same effective date) and return ``(row, None)``;
    * otherwise the lane's newest ``valid_from`` must be < the new one — back-dating into a
      closed period is refused with ValidationError (fix the dates by hand, we will not guess
      how to re-slice history);
    * the lane's open row (``valid_to IS NULL``, ``valid_from < new``) gets ``valid_to =
      new valid_from - 1 day`` so ranges stay contiguous and the exclusion constraint
      (general LIST lane) is satisfied before the insert.

    Runs in ``transaction.atomic`` so a constraint failure rolls back the close as well.
    Returns ``(new_or_updated_row, closed_row_or_None)``.
    """
    if isinstance(valid_from, str):
        valid_from = dt.date.fromisoformat(valid_from)
    if not isinstance(valid_from, dt.date):
        raise ValidationError('valid_from must be a date')
    try:
        price = q2(to_decimal(price, field='price'))
    except ValueError as exc:
        # Callers (views, MCP, seed) handle ValidationError; re-wrap so bad user input is a 400.
        raise ValidationError(str(exc)) from None
    if price < 0:
        raise ValidationError('price must be >= 0')
    # PriceListPrice.price is numeric(12,2): 10 digits before the point. Without this guard
    # Postgres raises DataError ("numeric field overflow"), which is NOT an IntegrityError and
    # so escapes the view's except clauses as a 500.
    if price >= MAX_PRICE:
        raise ValidationError(f'price must be less than {MAX_PRICE} (the column is numeric(12,2))')
    price_type = (price_type or 'LIST').upper()
    if price_type not in PRICE_TYPE_VALUES:
        raise ValidationError(f'price_type must be one of {list(PRICE_TYPE_VALUES)}')
    cid = _customer_id(customer)

    with transaction.atomic():
        lane = PriceListPrice.objects.select_for_update().filter(item=item, price_type=price_type)
        lane = lane.filter(customer_id=cid) if cid else lane.filter(customer__isnull=True)

        same_day = lane.filter(valid_from=valid_from).order_by('-id').first()
        if same_day is not None:
            same_day.price = price
            same_day.note = note or same_day.note
            same_day.set_by = set_by or same_day.set_by
            same_day.save(update_fields=['price', 'note', 'set_by'])
            return same_day, None

        newest = lane.order_by('-valid_from', '-id').first()
        if newest is not None and newest.valid_from > valid_from:
            raise ValidationError(
                f'{item.code} {price_type}{"/" + cid if cid else ""}: a price dated {newest.valid_from} already '
                f'exists; cannot back-date a new price to {valid_from}. Fix the dates by hand.'
            )

        # A CLOSED row may still cover the new date (e.g. a range closed by hand, or a lane
        # closed and later re-opened). The exclusion constraint only backstops the general LIST
        # lane, so for TRADE / SPECIAL / customer lanes this is the only thing standing between
        # us and two prices being valid on the same day.
        covering = (lane.filter(valid_from__lt=valid_from, valid_to__isnull=False, valid_to__gte=valid_from)
                        .order_by('-valid_from', '-id').first())
        if covering is not None:
            raise ValidationError(
                f'{item.code} {price_type}{"/" + cid if cid else ""}: {valid_from} falls inside the existing '
                f'period {covering.valid_from}..{covering.valid_to}. Close or correct that row first.'
            )

        closed = None
        open_row = lane.filter(valid_to__isnull=True, valid_from__lt=valid_from).order_by('-valid_from', '-id').first()
        if open_row is not None:
            open_row.valid_to = valid_from - dt.timedelta(days=1)
            open_row.save(update_fields=['valid_to'])
            closed = open_row

        created = PriceListPrice.objects.create(
            item=item, price=price, valid_from=valid_from, valid_to=None, price_type=price_type,
            customer_id=cid, note=note or '', set_by=set_by or '',
        )
        return created, closed


# --------------------------------------------------------------------------- #
# Quote builder (pure)
# --------------------------------------------------------------------------- #
def _num_str(d: Decimal) -> str:
    """'2' for whole numbers, '1.5' otherwise — never '2E+1' (Decimal.normalize's exponent form)."""
    return str(int(d)) if d == d.to_integral_value() else str(d)


def _nonneg_number(value, default, *, field: str) -> Decimal:
    if value is None or value == '':
        return Decimal(default)
    d = to_decimal(value, field=field)
    if d < 0:
        raise ValueError(f'{field} must be >= 0')
    return d


def build_quote(lines: Iterable[dict], *, customer=None, on_date: dt.date | None = None,
                discount_pct=Decimal('0'), vat_rate=VAT_RATE_DEFAULT) -> dict:
    """PURE CALCULATION — persists nothing.

    ``lines``: iterable of {'code', 'qty', 'days'}; qty/days default to 1, both must be >= 0.
    Per line: unit_price = resolved price; line_total = q2(unit_price * qty * days).
    An unknown code or an unresolvable price does NOT abort the quote — the line comes back
    with priced=False / unit_price=None / line_total '0.00' and a warning is appended.

      subtotal = sum(line_total)
      discount = q2(subtotal * discount_pct / 100)
      ex_vat   = subtotal - discount
      vat      = q2(ex_vat * vat_rate)
      incl_vat = ex_vat + vat

    All money serialised as 2-dp strings.
    """
    on_date = on_date or timezone.localdate()
    discount_pct = to_decimal(discount_pct, field='discount_pct')
    vat_rate = to_decimal(vat_rate, field='vat_rate')
    if discount_pct < 0 or discount_pct > 100:
        raise ValueError('discount_pct must be between 0 and 100')
    if vat_rate < 0:
        raise ValueError('vat_rate must be >= 0')
    cust = customer if isinstance(customer, XeroContacts) else None
    cid = _customer_id(customer)

    out_lines: list[dict] = []
    warnings: list[str] = []
    subtotal = Decimal('0')

    # One query for all the codes, then resolve per line (N small indexed queries — quotes are
    # a handful of lines, not thousands; clarity beats a prefetch here).
    lines = [dict(ln or {}) for ln in lines]
    codes = [str(ln.get('code') or '').strip().upper() for ln in lines]
    items = {i.code: i for i in PriceListItem.objects.filter(code__in=[c for c in codes if c])}

    for raw, code in zip(lines, codes):
        qty = _nonneg_number(raw.get('qty'), 1, field=f'qty for {code or "?"}')
        days = _nonneg_number(raw.get('days'), 1, field=f'days for {code or "?"}')
        item = items.get(code)
        line = {
            'code': code, 'name': None, 'unit': None, 'category': None,
            'qty': _num_str(qty),
            'days': _num_str(days),
            'unit_price': None, 'price_type': None, 'priced': False, 'line_total': '0.00',
            'valid_from': None, 'note': '',
        }
        if item is None:
            warnings.append(f'unknown item code {code!r}')
            out_lines.append(line)
            continue
        line.update({'name': item.name, 'unit': item.unit, 'category': item.category})
        row = get_price(item, on_date=on_date, customer=cid, price_type='LIST')
        if row is None:
            warnings.append(f'{code}: no price valid on {on_date.isoformat()}')
            out_lines.append(line)
            continue
        line_total = q2(row.price * qty * days)
        subtotal += line_total
        line.update({
            'unit_price': money(row.price), 'price_type': row.price_type, 'priced': True,
            'line_total': str(line_total), 'valid_from': row.valid_from.isoformat(), 'note': row.note,
        })
        if not item.active:
            warnings.append(f'{code} is marked inactive')
        out_lines.append(line)

    subtotal = q2(subtotal)
    discount = q2(subtotal * discount_pct / Decimal('100'))
    ex_vat = q2(subtotal - discount)
    vat = q2(ex_vat * vat_rate)
    incl_vat = q2(ex_vat + vat)
    return {
        'date': on_date.isoformat(),
        'customer_id': cid,
        'customer_name': cust.name if cust else None,
        'vat_rate': str(vat_rate),
        'discount_pct': str(discount_pct),
        'lines': out_lines,
        'subtotal': str(subtotal),
        'discount': str(discount),
        'ex_vat': str(ex_vat),
        'vat': str(vat),
        'incl_vat': str(incl_vat),
        'warnings': warnings,
    }


# --------------------------------------------------------------------------- #
# JSON shapes
# --------------------------------------------------------------------------- #
def _purchase_line_dict(line) -> dict | None:
    if line is None:
        return None
    inv = getattr(line, 'invoice', None)
    return {
        'id': line.id,
        'invoice_number': inv.invoice_number if inv is not None else None,
        'description': line.description,
        'line_amount': str(line.line_amount) if line.line_amount is not None else None,
        'account_code': line.account_code,
    }


def item_to_dict(item: PriceListItem, *, on_date: dt.date | None = None, customer=None) -> dict:
    """Item + resolved current LIST price (+ customer price when ``customer`` given).

    Callers listing many items should ``select_related('xero_purchase_line__invoice')`` — this
    function only guards against None, it does not fetch smartly.
    """
    on_date = on_date or timezone.localdate()
    current = get_price(item, on_date=on_date, customer=None, price_type='LIST')
    newest = item.prices.order_by('-valid_from', '-id').first()
    d: dict[str, Any] = {
        'code': item.code,
        'name': item.name,
        'category': item.category,
        'unit': item.unit,
        'qty_owned': item.qty_owned,
        'description': item.description,
        'active': item.active,
        'xero_account_code': item.xero_account_code,
        'xero_tracking_option_id': item.xero_tracking_option_id,
        'xero_purchase_line_id': item.xero_purchase_line_id,
        'xero_purchase_line': _purchase_line_dict(item.xero_purchase_line) if item.xero_purchase_line_id else None,
        'xero_fixed_asset_id': str(item.xero_fixed_asset_id) if item.xero_fixed_asset_id else None,
        'notes': item.notes,
        'created_at': item.created_at.isoformat() if item.created_at else None,
        'updated_at': item.updated_at.isoformat() if item.updated_at else None,
        'current_price': money(current.price) if current else None,
        'current_price_valid_from': current.valid_from.isoformat() if current else None,
        'last_changed': newest.valid_from.isoformat() if newest else None,
        'price_count': item.prices.count(),
    }
    if customer is not None:
        row = get_price(item, on_date=on_date, customer=customer, price_type='LIST')
        cust = customer if isinstance(customer, XeroContacts) else None
        d.update({
            'customer_price': money(row.price) if row else None,
            'customer_price_type': row.price_type if row else None,
            'customer_id': _customer_id(customer),
            'customer_name': cust.name if cust else (row.customer.name if row and row.customer else None),
        })
    return d


def price_to_dict(p: PriceListPrice) -> dict:
    return {
        'id': p.id,
        'price': money(p.price),
        'valid_from': p.valid_from.isoformat(),
        'valid_to': p.valid_to.isoformat() if p.valid_to else None,
        'price_type': p.price_type,
        'customer_id': p.customer_id,
        'customer_name': p.customer.name if p.customer_id and p.customer else None,
        'note': p.note,
        'set_by': p.set_by,
        'created_at': p.created_at.isoformat() if p.created_at else None,
    }
