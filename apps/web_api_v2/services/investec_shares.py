"""An entity's Investec share account: holdings and transactions.

Reads already-persisted rows only — no source-system call, no sync, no
mutation.

This is the Investec share ACCOUNT (the JSE stockbroking account whose data is
loaded from Investec). It is not the share analysis surface: market research,
valuation and portfolio-review screens are a different thing and do not belong
to this read.

Attribution follows the same discipline as the bank binding: an explicit,
reviewed InvestecEntityAccount row, never a name match. An entity without one
gets nothing rather than someone else's portfolio.
"""
import datetime
from decimal import Decimal

from django.db.models import Count, Max, Min, Q, Sum

from apps.investec.models import (
    InvestecEntityAccount,
    InvestecJsePortfolio,
    InvestecJseShareNameMapping,
    InvestecJseTransaction,
)

NOT_BOUND_REASON = (
    'No Investec share account is bound to this entity. Binding one attributes '
    'a real portfolio to this entity\'s books, so it is set only once the '
    'ownership has been confirmed.'
)
NO_DATA_REASON = (
    'This entity is bound to an Investec share account, but no holdings or '
    'transactions have been loaded for it yet.'
)


DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def bound_accounts(entity_id):
    """Every share account bound to this entity, or None if none are.

    Plural, like the bank side. An entity can hold more than one stockbroking
    account — Investec renumbers them, and a renumbering leaves the history
    under the old number. Reading only the first bound account silently hid
    everything before the change, which is exactly how six years of a portfolio
    can vanish from a screen that looks like it is working.

    Binding attributes a real portfolio to a real company's books, so it is a
    reviewed row in InvestecEntityAccount rather than a constant in code — the
    people who know the ownership should not need a deploy to record it.
    """
    numbers = InvestecEntityAccount.numbers_for(entity_id, InvestecEntityAccount.Kind.SHARE)
    return list(numbers) if numbers else None



def read_share_account(
    entity_id,
    selected_periods,
    *,
    limit=DEFAULT_LIMIT,
    offset=0,
    search='',
    types=None,
):
    account_numbers = bound_accounts(entity_id)
    if account_numbers is None:
        return _empty(NOT_BOUND_REASON, None)

    transactions = InvestecJseTransaction.objects.filter(account_number__in=account_numbers)
    if not transactions.exists() and not InvestecJsePortfolio.objects.exists():
        return _empty(NO_DATA_REASON, _mask_all(account_numbers))

    # Holdings are a position at a date, not a period aggregate: the latest
    # loaded snapshot is the only truthful "what is held" answer, and summing
    # snapshots across a period would multiply the portfolio.
    holdings_as_at = InvestecJsePortfolio.objects.aggregate(latest=Max('date'))['latest']
    holdings = list(
        InvestecJsePortfolio.objects.filter(date=holdings_as_at).order_by('-total_value')
    ) if holdings_as_at else []

    period_transactions = transactions.filter(_selected_period_filter_dates(selected_periods))
    totals = period_transactions.aggregate(
        count=Count('pk'), net_value=Sum('value'),
        earliest=Min('date'), latest=Max('date'),
    )

    # The filtered set is a different fact from the period set, so it is a
    # different number. Reporting one as the other would make a search look
    # like the account had shrunk.
    matching = _apply_filters(period_transactions, search, types)
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    page = matching.order_by('-date', '-pk')[offset:offset + limit]

    return {
        'available': True,
        'userSafeReason': None,
        'accountMasked': _mask_all(account_numbers),
        # This account is loaded by uploading statements and mapping each share
        # name to a share code. A name with no mapping leaves its transactions
        # unattributable to a holding, so the gap is reported rather than left
        # to be noticed later.
        'unmappedShareNames': _unmapped_share_names(period_transactions),
        'holdingsAsAt': holdings_as_at,
        'holdings': [{
            'id': str(row.pk),
            'shareCode': row.share_code or '',
            'company': row.company or '',
            'quantity': row.quantity,
            'currency': row.currency or 'ZAR',
            'totalCost': row.total_cost,
            'totalValue': row.total_value,
        } for row in holdings],
        'holdingsValue': sum((row.total_value for row in holdings), Decimal('0')),
        # Everything in the period, whatever the filter says.
        'transactionCount': totals['count'] or 0,
        # Everything matching the filter — what the pager counts through.
        'filteredCount': matching.count(),
        # Offered as filter options: the types actually present in the period,
        # so the control can never offer a choice that returns nothing.
        # order_by() clears the model's Meta ordering before DISTINCT. Without
        # it Django adds the ordering columns to the SELECT, so the distinct
        # runs over (type, date, created_at) and returns one entry per ROW —
        # 104 "distinct" types for 13 real ones, and a filter control listing
        # Dividend forty times.
        'transactionTypes': sorted(
            value for value in period_transactions
            .exclude(type='').exclude(type__isnull=True)
            .order_by().values_list('type', flat=True).distinct()
        ),
        'summary': {
            'transactionCount': totals['count'] or 0,
            'netValue': totals['net_value'],
            'earliestDate': totals['earliest'],
            'latestDate': totals['latest'],
        },
        'transactions': [{
            'id': str(row.pk),
            'date': row.date,
            'type': row.type or '',
            'shareName': row.share_name or '',
            'description': row.description or '',
            'quantity': row.quantity,
            'value': row.value,
        } for row in page],
    }


def _apply_filters(queryset, search, types):
    """Narrow a period's transactions by free text and by type.

    Search covers the three fields a reader can actually see on the row — the
    share name, the description and the type — so a search that matches nothing
    visible is not silently matching something hidden.
    """
    selected = [value for value in (types or []) if value]
    if selected:
        queryset = queryset.filter(type__in=selected)
    term = (search or '').strip()
    if term:
        queryset = queryset.filter(
            Q(share_name__icontains=term)
            | Q(description__icontains=term)
            | Q(type__icontains=term)
        )
    return queryset


def _empty(reason, account_masked):
    return {
        'available': False, 'userSafeReason': reason, 'accountMasked': account_masked,
        'holdings': [], 'transactions': [], 'holdingsAsAt': None, 'summary': None,
        'transactionCount': 0, 'filteredCount': 0, 'transactionTypes': [],
        'unmappedShareNames': [], 'holdingsValue': None,
    }


def _mask_all(account_numbers):
    """Every bound account, masked. Plural because an entity may hold several."""
    return ', '.join(_mask(number) for number in account_numbers) or None


def _selected_period_filter_dates(selected_periods):
    """The bank filter targets transaction_date; JSE rows use `date`."""
    import calendar

    query = Q()
    for period in selected_periods:
        year, month = (int(value) for value in str(period).split('-', 1))
        starts_on = datetime.date(year, month, 1)
        ends_on = datetime.date(year, month, calendar.monthrange(year, month)[1])
        query |= Q(date__range=(starts_on, ends_on))
    return query


def _mask(account_number):
    digits = ''.join(character for character in str(account_number or '') if character.isdigit())
    return f'•••• {digits[-4:]}' if len(digits) >= 4 else '•••• ????'


def _unmapped_share_names(transactions):
    """Share names in these transactions that no mapping resolves to a code.

    A mapping row carries up to three names for one share_code — share_name,
    share_name2 and share_name3 — because the same instrument arrives under
    different spellings on different statements ("A V I" and "AVI" are one
    share). Checking only share_name reports names that are in fact mapped,
    which turns a data-quality screen into a source of false work.

    Account-level rows (fees, interest) legitimately carry no share name and
    are not counted: they are not a mapping gap.
    """
    names = {
        name.strip()
        for name in transactions.values_list('share_name', flat=True)
        if name and name.strip()
    }
    if not names:
        return []

    resolved = (
        InvestecJseShareNameMapping.objects
        .exclude(share_code__isnull=True)
        .exclude(share_code='')
        .filter(
            Q(share_name__in=names)
            | Q(share_name2__in=names)
            | Q(share_name3__in=names)
        )
        .values_list('share_name', 'share_name2', 'share_name3')
    )
    mapped = {
        value
        for row in resolved
        for value in row
        if value
    }
    return sorted(names - mapped)
