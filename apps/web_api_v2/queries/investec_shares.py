from apps.web_api_v2.queries.xero_pipeline import _graphql_error
from apps.web_api_v2.services.entity_access import (
    VIEW_FINANCIALS_CAPABILITY,
    capability_codes_for_membership,
)
from apps.web_api_v2.services.fiscal_context import resolve_financial_context
from apps.web_api_v2.services.investec_shares import read_share_account
from apps.web_api_v2.types.investec_shares import (
    InvestecShareAccount,
    InvestecShareHolding,
    InvestecShareSummary,
    InvestecShareTransactionRow,
)


def build_investec_share_account(info, context_input, limit, offset=0, search='', types=None):
    membership, context = resolve_financial_context(info, context_input)
    if VIEW_FINANCIALS_CAPABILITY not in capability_codes_for_membership(membership):
        _graphql_error(info, 'CAPABILITY_REQUIRED', 'VIEW_FINANCIALS capability is required.')

    periods = [str(period) for period in context.selected_periods]
    result = read_share_account(
        membership.entity.pk, periods,
        limit=limit, offset=offset, search=search, types=types,
    )
    summary = result['summary']
    return InvestecShareAccount(
        context=context,
        available=result['available'],
        user_safe_reason=result['userSafeReason'],
        account_masked=result['accountMasked'],
        holdings_as_at=result['holdingsAsAt'],
        holdings_value=result.get('holdingsValue'),
        holdings=[InvestecShareHolding(
            id=row['id'], share_code=row['shareCode'], company=row['company'],
            quantity=row['quantity'], currency=row['currency'],
            total_cost=row['totalCost'], total_value=row['totalValue'],
        ) for row in result['holdings']],
        summary=InvestecShareSummary(
            transaction_count=summary['transactionCount'],
            net_value=summary['netValue'],
            earliest_date=summary['earliestDate'],
            latest_date=summary['latestDate'],
        ) if summary else None,
        transaction_count=result['transactionCount'],
        filtered_count=result['filteredCount'],
        transaction_types=result['transactionTypes'],
        unmapped_share_names=result.get('unmappedShareNames') or [],
        transactions=[InvestecShareTransactionRow(
            id=row['id'], date=row['date'], type=row['type'],
            share_name=row['shareName'], description=row['description'],
            quantity=row['quantity'], value=row['value'],
        ) for row in result['transactions']],
    )
