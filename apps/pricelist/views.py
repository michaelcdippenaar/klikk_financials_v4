"""
REST surface for the equipment price list — consumed by the klikk-financials
MCP server and the console; usable directly.

GET   /api/pricelist/items/?category=PA&active=1&q=d%26b&customer=<id|name>   list items (+ customer price)
POST  /api/pricelist/items/                                                   upsert item by code (409 unless replace=true)
GET   /api/pricelist/items/<code>/?date=&customer=                            one item
PATCH /api/pricelist/items/<code>/   (PUT alias)                               partial update of item fields (never prices)
GET   /api/pricelist/items/<code>/price/?date=&customer=&type=                 resolve the price that applies
GET   /api/pricelist/items/<code>/prices/                                      price history, newest first
POST  /api/pricelist/items/<code>/prices/  {price, valid_from, price_type?, customer?, note?, set_by?}
                                                                              add effective-dated price (closes previous)
POST  /api/pricelist/quote/  {lines:[{code,qty,days}], customer?, date?, discount_pct?, vat_rate?}
                                                                              pure quote calc — persists nothing
GET   /api/pricelist/export/?date=&customer=&active=                          CSV of the rate card

The three write actions — POST /items/, PATCH|PUT /items/<code>/ and
POST /items/<code>/prices/ — require either Authorization: Bearer <KLIKK_API_TOKEN>
(the machine callers: the klikk-financials MCP server) or a logged-in console user,
whose JWT satisfies the same permission. Every GET, and POST /quote/ (a pure
calculation that persists nothing), stay open — matching the rest of the backend.
See klikk_business_intelligence/permissions.py for why the token needs an
authentication class and not merely a permission class.

Writes touch only pricelist_* tables. Xero is never called; customer / purchase
line / tracking lookups are against the local mirror.
"""
import csv
import datetime as dt
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from klikk_business_intelligence.permissions import HasServiceToken

from .models import (
    CATEGORY_VALUES, PRICE_TYPE_VALUES, UNIT_VALUES, PriceListItem,
)
from .services import (
    VAT_RATE_DEFAULT, add_price, build_quote, customer_to_dict, get_price, item_to_dict, price_to_dict,
    resolve_customer, resolve_price, to_decimal,
)

# Item fields a client may set via POST/PATCH. Prices go through /prices/ only — so the
# effective-dating rules in services.add_price can never be bypassed.
ITEM_FIELDS = (
    'name', 'category', 'unit', 'qty_owned', 'description', 'active',
    'xero_account_code', 'xero_tracking_option_id', 'xero_purchase_line_id', 'xero_fixed_asset_id', 'notes',
)
TRUE_VALUES = ('1', 'true', 'True', 'yes', 'on')
FALSE_VALUES = ('0', 'false', 'False', 'no', 'off')


class _BadRequest(Exception):
    """Raised by the parsing helpers; views turn it into a 400 with the message."""


def _date(value, default):
    """ISO date or the default. _BadRequest on a malformed value (never 500 on user input)."""
    if value in (None, ''):
        return default
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        raise _BadRequest(f'bad date {value!r}; expected YYYY-MM-DD') from None


def _decimal(value, default, *, field):
    if value in (None, ''):
        return default
    try:
        return to_decimal(value, field=field)
    except ValueError as exc:
        raise _BadRequest(str(exc)) from None


def _bool(value, default=None):
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip()
    if s in TRUE_VALUES:
        return True
    if s in FALSE_VALUES:
        return False
    raise _BadRequest(f'bad boolean {value!r}; use 1/0 or true/false')


def _truthy(value) -> bool:
    return value is True or str(value).strip() in TRUE_VALUES


def _customer(value):
    """resolve_customer, with BOTH failure modes mapped to a 400.

    An unresolvable customer must never fall through to "no customer": that would quietly
    answer a question about Aurras' rate with the LIST price, and the caller (console, MCP,
    curl) would have no way to tell. A typo has to be an error, not a different answer.
    """
    try:
        customer = resolve_customer(value)
    except ValueError as exc:
        raise _BadRequest(str(exc)) from None
    if value not in (None, '') and customer is None:
        raise _BadRequest(f'unknown customer {value!r}')
    return customer


def _item_or_404(code):
    return PriceListItem.objects.select_related('xero_purchase_line__invoice').filter(code=code.strip().upper()).first()


def _apply_item_fields(item, data, *, partial):
    """Validate + assign whitelisted item fields from a request body. Raises _BadRequest."""
    for key in ITEM_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if key == 'category':
            value = str(value or '').strip().upper()
            if value not in CATEGORY_VALUES:
                raise _BadRequest(f'category must be one of {list(CATEGORY_VALUES)}')
        elif key == 'unit':
            value = str(value or '').strip().upper()
            if value not in UNIT_VALUES:
                raise _BadRequest(f'unit must be one of {list(UNIT_VALUES)}')
        elif key == 'qty_owned':
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise _BadRequest('qty_owned must be an integer') from None
            if value < 0:
                raise _BadRequest('qty_owned must be >= 0')
        elif key == 'active':
            value = _bool(value, True)
        elif key == 'xero_purchase_line_id':
            if value in (None, ''):
                value = None
            else:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise _BadRequest('xero_purchase_line_id must be an integer') from None
        elif key == 'xero_fixed_asset_id':
            if value in (None, ''):
                value = None
            else:
                try:
                    value = uuid.UUID(str(value).strip())
                except ValueError:
                    raise _BadRequest('xero_fixed_asset_id must be a UUID') from None
        else:
            value = '' if value is None else str(value)
        setattr(item, key, value)
    if not partial and not (item.name or '').strip():
        raise _BadRequest('name is required')


# --------------------------------------------------------------------------- #
# /items/
# --------------------------------------------------------------------------- #
# SECURITY (2026-08-19): the three write actions below carry HasServiceToken. They are
# internet-reachable via the Caddy edge (console.8-bit.space/backend/*) and they mutate the
# rate card, so an unauthenticated caller must not reach them. Two callers are allowed: the
# klikk-financials MCP server, which sends Authorization: Bearer <KLIKK_API_TOKEN>, and the
# console, which sends a Bearer JWT (klikk_portal src/api/client.js). Reads and POST /quote/
# are deliberately NOT covered — locking those is a project-wide decision, not a pricelist
# one. Do NOT drop these decorators.
@api_view(['GET', 'POST'])
@permission_classes([HasServiceToken])
def items_view(request):
    if request.method == 'GET':
        try:
            on_date = _date(request.query_params.get('date'), timezone.localdate())
            active = _bool(request.query_params.get('active'))
            customer = _customer(request.query_params.get('customer'))
        except _BadRequest as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        qs = PriceListItem.objects.select_related('xero_purchase_line__invoice')
        category = (request.query_params.get('category') or '').strip().upper()
        if category:
            qs = qs.filter(category=category)
        if active is not None:
            qs = qs.filter(active=active)
        q = (request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q) | Q(description__icontains=q))
        items = [item_to_dict(i, on_date=on_date, customer=customer) for i in qs]
        categories = sorted({i['category'] for i in items})
        return Response({'count': len(items), 'categories': categories,
                         'customer': customer_to_dict(customer), 'items': items})

    # POST — upsert by code
    data = request.data or {}
    code = str(data.get('code') or '').strip().upper()
    if not code:
        return Response({'detail': 'code is required'}, status=status.HTTP_400_BAD_REQUEST)
    if not str(data.get('name') or '').strip():
        return Response({'detail': 'name is required'}, status=status.HTTP_400_BAD_REQUEST)
    existing = PriceListItem.objects.filter(code=code).first()
    if existing is not None and not _truthy(data.get('replace')):
        return Response({'detail': f'item {code} already exists (pass replace=true to update it)'},
                        status=status.HTTP_409_CONFLICT)
    item = existing or PriceListItem(code=code)
    try:
        _apply_item_fields(item, data, partial=False)
        price = data.get('price')
        valid_from = _date(data.get('valid_from'), None)
        if (price not in (None, '')) != (valid_from is not None):
            raise _BadRequest('price and valid_from must be given together')
        price_row = None
        # One unit of work: an item must not be left behind when its opening price is rejected.
        with transaction.atomic():
            item.save()
            if valid_from is not None:
                price_row, _closed = add_price(item, price=price, valid_from=valid_from, price_type='LIST',
                                               set_by=str(data.get('set_by') or ''),
                                               note=str(data.get('note') or ''))
    except _BadRequest as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except ValidationError as exc:
        return Response({'detail': '; '.join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
    except (IntegrityError, DataError) as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    item = _item_or_404(code)
    return Response({'created': existing is None, 'item': item_to_dict(item),
                     'price': price_to_dict(price_row) if price_row else None},
                    status=status.HTTP_201_CREATED if existing is None else status.HTTP_200_OK)


# --------------------------------------------------------------------------- #
# /items/<code>/
# --------------------------------------------------------------------------- #
@api_view(['GET', 'PATCH', 'PUT'])
@permission_classes([HasServiceToken])
def item_detail_view(request, code):
    item = _item_or_404(code)
    if item is None:
        return Response({'detail': f'unknown item {code}'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'GET':
        try:
            on_date = _date(request.query_params.get('date'), timezone.localdate())
            customer = _customer(request.query_params.get('customer'))
        except _BadRequest as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(item_to_dict(item, on_date=on_date, customer=customer))

    # PATCH / PUT — both partial: the rate card is edited field-by-field from the console and
    # the MCP tool; a "replace everything" PUT would silently blank the Xero links.
    data = request.data or {}
    if 'code' in data and str(data['code']).strip().upper() != item.code:
        return Response({'detail': 'code cannot be changed (it is the public identifier)'},
                        status=status.HTTP_400_BAD_REQUEST)
    for forbidden in ('price', 'valid_from', 'prices'):
        if forbidden in data:
            return Response({'detail': f'{forbidden} cannot be set here — POST /items/{item.code}/prices/'},
                            status=status.HTTP_400_BAD_REQUEST)
    try:
        _apply_item_fields(item, data, partial=True)
        item.save()
    except _BadRequest as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except (IntegrityError, DataError) as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(item_to_dict(_item_or_404(item.code)))


@api_view(['GET'])
def item_price_view(request, code):
    item = _item_or_404(code)
    if item is None:
        return Response({'detail': f'unknown item {code}'}, status=status.HTTP_404_NOT_FOUND)
    try:
        on_date = _date(request.query_params.get('date'), timezone.localdate())
        customer = _customer(request.query_params.get('customer'))
    except _BadRequest as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    price_type = (request.query_params.get('type') or 'LIST').strip().upper()
    if price_type not in PRICE_TYPE_VALUES:
        return Response({'detail': f'type must be one of {list(PRICE_TYPE_VALUES)}'}, status=status.HTTP_400_BAD_REQUEST)
    return Response(resolve_price(item, on_date=on_date, customer=customer, price_type=price_type))


@api_view(['GET', 'POST'])
@permission_classes([HasServiceToken])
def item_prices_view(request, code):
    item = _item_or_404(code)
    if item is None:
        return Response({'detail': f'unknown item {code}'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'GET':
        rows = item.prices.select_related('customer').order_by('-valid_from', '-id')
        prices = [price_to_dict(p) for p in rows]
        return Response({'code': item.code, 'count': len(prices), 'prices': prices})

    data = request.data or {}
    if data.get('price') in (None, '') or not data.get('valid_from'):
        return Response({'detail': 'price and valid_from are required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        valid_from = _date(data.get('valid_from'), None)
        customer = _customer(data.get('customer'))
        price_type = (str(data.get('price_type') or 'LIST')).strip().upper()
        if price_type not in PRICE_TYPE_VALUES:
            raise _BadRequest(f'price_type must be one of {list(PRICE_TYPE_VALUES)}')
        row, closed = add_price(
            item, price=data.get('price'), valid_from=valid_from, price_type=price_type, customer=customer,
            note=str(data.get('note') or ''), set_by=str(data.get('set_by') or ''),
        )
    except _BadRequest as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except ValidationError as exc:
        return Response({'detail': '; '.join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
    except (IntegrityError, DataError) as exc:
        # The exclusion constraint is the backstop for anything add_price's lane logic did not
        # foresee (e.g. a hand-edited row); surface the DB message rather than 500.
        return Response({'detail': f'price rejected by the database: {exc}'}, status=status.HTTP_400_BAD_REQUEST)
    return Response({'price': price_to_dict(row), 'closed_previous': price_to_dict(closed) if closed else None},
                    status=status.HTTP_201_CREATED)


# --------------------------------------------------------------------------- #
# /quote/
# --------------------------------------------------------------------------- #
@api_view(['POST'])
def quote_view(request):
    data = request.data or {}
    lines = data.get('lines')
    if not isinstance(lines, list) or not lines:
        return Response({'detail': 'lines must be a non-empty list of {code, qty, days}'},
                        status=status.HTTP_400_BAD_REQUEST)
    if not all(isinstance(ln, dict) for ln in lines):
        return Response({'detail': 'each line must be an object {code, qty, days}'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        on_date = _date(data.get('date'), timezone.localdate())
        customer = _customer(data.get('customer'))
        discount_pct = _decimal(data.get('discount_pct'), Decimal('0'), field='discount_pct')
        vat_rate = _decimal(data.get('vat_rate'), VAT_RATE_DEFAULT, field='vat_rate')
        quote = build_quote(lines, customer=customer, on_date=on_date, discount_pct=discount_pct, vat_rate=vat_rate)
    except _BadRequest as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(quote)


# --------------------------------------------------------------------------- #
# /export/
# --------------------------------------------------------------------------- #
EXPORT_COLUMNS = (
    'code', 'name', 'category', 'unit', 'qty_owned', 'current_price_ex_vat', 'price_valid_from', 'customer_price',
    'xero_account_code', 'xero_tracking_option_id', 'xero_purchase_line_id', 'active', 'notes',
)


@api_view(['GET'])
def export_view(request):
    try:
        on_date = _date(request.query_params.get('date'), timezone.localdate())
        active = _bool(request.query_params.get('active'))
        customer = _customer(request.query_params.get('customer'))
    except _BadRequest as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    # The console's "Export CSV" button sits next to the category / search filters, so the
    # export has to honour them — otherwise MC filters to PROJECTOR, exports, and silently
    # gets the whole rate card. Same filter semantics as GET /items/.
    qs = PriceListItem.objects.all()
    if active is not None:
        qs = qs.filter(active=active)
    category = (request.query_params.get('category') or '').strip().upper()
    if category:
        qs = qs.filter(category=category)
    q = (request.query_params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q) | Q(description__icontains=q))

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="klikk-pricelist-{on_date.isoformat()}.csv"'
    writer = csv.writer(response)
    writer.writerow(EXPORT_COLUMNS)
    for item in qs:
        current = get_price(item, on_date=on_date, customer=None, price_type='LIST')
        cust_row = get_price(item, on_date=on_date, customer=customer, price_type='LIST') if customer else None
        writer.writerow([
            item.code, item.name, item.category, item.unit, item.qty_owned,
            str(current.price) if current else '',
            current.valid_from.isoformat() if current else '',
            str(cust_row.price) if cust_row else '',
            item.xero_account_code, item.xero_tracking_option_id,
            item.xero_purchase_line_id or '',
            '1' if item.active else '0',
            item.notes,
        ])
    return response
