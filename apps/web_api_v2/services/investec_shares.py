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


def bound_account(entity_id):
    """The share account bound to this entity, or None.

    Binding attributes a real portfolio to a real company's books, so it is a
    reviewed row in InvestecEntityAccount rather than a constant in code — the
    people who know the ownership should not need a deploy to record it.
    """
    numbers = InvestecEntityAccount.numbers_for(entity_id, InvestecEntityAccount.Kind.SHARE)
    return numbers[0] if numbers else None


def read_share_account(entity_id, selected_periods, *, limit=100):
    account_number = bound_account(entity_id)
    if account_number is None:
        return {'available': False, 'userSafeReason': NOT_BOUND_REASON,
                'accountMasked': None, 'holdings': [], 'transactions': [],
                'holdingsAsAt': None, 'summary': None, 'transactionCount': 0}

    transactions = InvestecJseTransaction.objects.filter(account_number=account_number)
    if not transactions.exists() and not InvestecJsePortfolio.objects.exists():
        return {'available': False, 'userSafeReason': NO_DATA_REASON,
                'accountMasked': _mask(account_number), 'holdings': [], 'transactions': [],
                'holdingsAsAt': None, 'summary': None, 'transactionCount': 0}

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

    return {
        'available': True,
        'userSafeReason': None,
        'accountMasked': _mask(account_number),
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
        'transactionCount': totals['count'] or 0,
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
        } for row in period_transactions.order_by('-date', '-pk')[:limit]],
    }


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
