from apps.web_api_v2.queries.xero_pipeline import _graphql_error
from apps.web_api_v2.services.entity_access import (
    VIEW_FINANCIALS_CAPABILITY,
    capability_codes_for_membership,
)
from apps.web_api_v2.services.fiscal_context import resolve_financial_context
from apps.web_api_v2.services.investec_bank_transactions import read_bank_transactions
from apps.web_api_v2.types.investec_bank import (
    InvestecBankAccountRef,
    InvestecBankSummary,
    InvestecBankTransactionRow,
    InvestecBankTransactions,
)


def build_investec_bank_transactions(info, context_input, limit, offset):
    membership, context = resolve_financial_context(info, context_input)
    if VIEW_FINANCIALS_CAPABILITY not in capability_codes_for_membership(membership):
        _graphql_error(info, 'CAPABILITY_REQUIRED', 'VIEW_FINANCIALS capability is required.')

    periods = [str(period) for period in context.selected_periods]
    result = read_bank_transactions(
        membership.entity.pk, periods, limit=limit, offset=offset,
    )
    summary = result['summary']
    return InvestecBankTransactions(
        context=context,
        available=result['available'],
        user_safe_reason=result['userSafeReason'],
        accounts=[InvestecBankAccountRef(
            id=account['id'],
            masked_number=account['maskedNumber'],
            name=account['name'],
        ) for account in result['accounts']],
        summary=InvestecBankSummary(
            transaction_count=summary['transactionCount'],
            net_amount=summary['netAmount'],
            earliest_date=summary['earliestDate'],
            latest_date=summary['latestDate'],
            freshness_at=summary['freshnessAt'],
        ) if summary else None,
        total_count=result['totalCount'],
        rows=[InvestecBankTransactionRow(
            id=row['id'],
            date=row['date'],
            account_masked=row['accountMasked'],
            description=row['description'],
            transaction_type=row['transactionType'],
            status=row['status'],
            amount=row['amount'],
            running_balance=row['runningBalance'],
        ) for row in result['rows']],
    )
