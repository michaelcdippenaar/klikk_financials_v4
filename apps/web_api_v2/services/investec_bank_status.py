import calendar
import datetime

from django.db.models import Count, Max, Q

from apps.investec.models import InvestecBankAccount, InvestecBankTransaction
from apps.investec.owner_map import INVESTEC_OWNER_MAP, KLIKK


# This is an explicit cross-system identity binding, not a name match. The Xero
# tenant id is the durable V2 entity identity and the owner-map label is the
# existing durable attribution used for Investec bank accounts. New entities
# remain unbound until an equally explicit mapping is reviewed.
INVESTEC_BANK_ENTITY_BINDINGS = {
    '41ebfa0e-012e-4ff1-82ba-a9a7585c536c': KLIKK,
}


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
    owner = INVESTEC_BANK_ENTITY_BINDINGS.get(str(entity_id))
    if owner is None:
        return None

    account_numbers = tuple(
        account_number
        for account_number, attribution in INVESTEC_OWNER_MAP.items()
        if attribution['entity'] == owner
    )
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
