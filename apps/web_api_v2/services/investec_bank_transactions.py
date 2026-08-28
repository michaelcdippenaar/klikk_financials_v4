"""Period-scoped Investec bank transactions for an entity.

Reads already-persisted rows only: no source-system call, no sync, no
mutation. The entity binding is the same explicit cross-system mapping the
status read uses — a Xero tenant id mapped to an Investec owner label — and an
entity without one returns nothing rather than falling back to a name match.

Account numbers are masked to their last four digits. The screen needs to tell
one account from another, which four digits do; it does not need the number
itself, and a full bank account number on a page is a liability with no
corresponding benefit.
"""
from django.db.models import Count, Max, Min, Sum

from apps.investec.models import InvestecBankAccount, InvestecBankTransaction
from apps.investec.owner_map import INVESTEC_OWNER_MAP
from apps.web_api_v2.services.investec_bank_status import (
    INVESTEC_BANK_ENTITY_BINDINGS,
    _selected_period_filter,
)

# A page of transactions. A Klikk month runs to a few hundred rows; the cap is
# a ceiling on one read, not a claim about how many exist.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

NOT_BOUND_REASON = (
    'No Investec bank account is bound to this entity, so there are no '
    'transactions to show for it.'
)
NO_ACCOUNTS_REASON = (
    'This entity is bound to an Investec owner, but no matching bank account '
    'has been synced into Klikk Financials yet.'
)


def mask_account_number(account_number):
    """Last four digits only. Distinguishing accounts does not require the number."""
    digits = ''.join(character for character in str(account_number or '') if character.isdigit())
    return f'•••• {digits[-4:]}' if len(digits) >= 4 else '•••• ????'


def entity_accounts(entity_id):
    owner = INVESTEC_BANK_ENTITY_BINDINGS.get(str(entity_id))
    if owner is None:
        return None
    account_numbers = tuple(
        account_number
        for account_number, attribution in INVESTEC_OWNER_MAP.items()
        if attribution['entity'] == owner
    )
    return list(
        InvestecBankAccount.objects.filter(account_number__in=account_numbers)
        .order_by('account_number')
    )


def read_bank_transactions(entity_id, selected_periods, *, limit=DEFAULT_LIMIT, offset=0):
    accounts = entity_accounts(entity_id)
    if accounts is None:
        return {'available': False, 'userSafeReason': NOT_BOUND_REASON,
                'accounts': [], 'totalCount': 0, 'rows': [], 'summary': None}
    if not accounts:
        return {'available': False, 'userSafeReason': NO_ACCOUNTS_REASON,
                'accounts': [], 'totalCount': 0, 'rows': [], 'summary': None}

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    by_id = {account.pk: account for account in accounts}

    queryset = (
        InvestecBankTransaction.objects
        .filter(account_id__in=list(by_id))
        .filter(_selected_period_filter(selected_periods))
    )
    totals = queryset.aggregate(
        count=Count('pk'),
        money_in=Sum('amount', filter=None),
        earliest=Min('transaction_date'),
        latest=Max('transaction_date'),
        freshness=Max('updated_at'),
    )
    rows = list(
        queryset.order_by('-transaction_date', '-posted_order', '-pk')[offset:offset + limit]
    )

    return {
        'available': True,
        'userSafeReason': None,
        'accounts': [{
            'id': str(account.pk),
            'maskedNumber': mask_account_number(account.account_number),
            'name': account.account_name or '',
        } for account in accounts],
        'totalCount': totals['count'] or 0,
        'summary': {
            'transactionCount': totals['count'] or 0,
            'netAmount': totals['money_in'],
            'earliestDate': totals['earliest'],
            'latestDate': totals['latest'],
            'freshnessAt': totals['freshness'],
        },
        'rows': [{
            'id': str(row.pk),
            'date': row.transaction_date,
            'accountMasked': mask_account_number(by_id[row.account_id].account_number),
            'description': row.description or '',
            'transactionType': row.transaction_type or '',
            'status': row.status or '',
            'amount': row.amount,
            'runningBalance': row.running_balance,
            # No V2 contract validates a bank transaction against anything, so
            # there is deliberately no validation or evidence field here. An
            # empty one would read as "checked, nothing found".
        } for row in rows],
    }
