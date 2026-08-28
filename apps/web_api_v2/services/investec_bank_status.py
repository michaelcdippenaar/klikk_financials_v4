import calendar
import datetime

from django.db.models import Count, Max, Q

from apps.investec.models import (
    InvestecBankAccount,
    InvestecBankTransaction,
    InvestecEntityAccount,
)


# The binding lives in InvestecEntityAccount, not in this module. It used to be
# a dict here, which meant attributing a bank account — an ownership fact —
# needed a code change and a deploy. It is still an explicit reviewed binding,
# never a name match; an entity with no rows sees nothing.
def bank_account_numbers(entity_id):
    return InvestecEntityAccount.numbers_for(entity_id, InvestecEntityAccount.Kind.BANK)


def _month_window(period):
    year, month = (int(value) for value in str(period).split('-', 1))
    starts_on = datetime.date(year, month, 1)
    ends_on = datetime.date(year, month, calendar.monthrange(year, month)[1])
    return starts_on, ends_on


def _selected_period_filter(selected_periods):
    query = Q()
    for period in selected_periods:
        starts_on, ends_on = _month_window(period)
        query |= Q(transaction_date__range=(starts_on, ends_on))
    return query


def read_investec_bank_status(entity_id, selected_periods):
    """Return entity-safe, period-scoped local Investec bank evidence.

    This reads only already-persisted rows. It performs no source-system call,
    sync, mutation, or fallback name matching.
    """
    account_numbers = bank_account_numbers(entity_id)
    if not account_numbers:
        return None

    account_ids = InvestecBankAccount.objects.filter(
        account_number__in=account_numbers,
    ).values_list('pk', flat=True)
    if not account_ids.exists():
        return {
            'configured': False,
            'records_read': None,
            'freshness_at': None,
        }

    transactions = InvestecBankTransaction.objects.filter(
        account_id__in=account_ids,
    ).filter(_selected_period_filter(selected_periods))
    evidence = transactions.aggregate(
        records_read=Count('pk'),
        freshness_at=Max('updated_at'),
    )
    return {
        'configured': True,
        'records_read': evidence['records_read'],
        'freshness_at': evidence['freshness_at'],
    }
