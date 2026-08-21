import datetime

import strawberry
from graphql import GraphQLError

from apps.web_api_v2.services.entity_access import require_entity_access
from apps.web_api_v2.types.financial_context import (
    FinancialPeriodSelectionMode,
    ResolvedFinancialContext,
)
from apps.web_api_v2.types.ingest import YearMonthValue


def _validation_error(info, message):
    request = info.context.request
    raise GraphQLError(
        message,
        extensions={
            'code': 'VALIDATION_ERROR',
            'correlationId': getattr(request, 'graphql_correlation_id', '-'),
            'retryable': False,
        },
    )


def _month_key(year, month):
    return f'{year:04d}-{month:02d}'


def _fiscal_periods(financial_year, start_month):
    start_year = financial_year if start_month == 1 else financial_year - 1
    periods = []
    year = start_year
    month = start_month
    for _ in range(12):
        periods.append(_month_key(year, month))
        month += 1
        if month == 13:
            month = 1
            year += 1
    return periods


def resolve_financial_context(info, input_value):
    membership = require_entity_access(info, input_value.entity_id)
    financial_year = input_value.financial_year
    if isinstance(financial_year, bool) or not 1901 <= financial_year <= 9999:
        _validation_error(info, 'financialYear must be between 1901 and 9999.')

    start_month = membership.entity.get_fiscal_year_start_month()
    all_periods = _fiscal_periods(financial_year, start_month)
    supplied = [str(value) for value in (input_value.period_selection.months or [])]
    mode = input_value.period_selection.mode
    if mode == FinancialPeriodSelectionMode.ALL:
        if supplied:
            _validation_error(info, 'months must be omitted when periodSelection mode is ALL.')
        selected = all_periods
    else:
        if not 1 <= len(supplied) <= 12 or len(set(supplied)) != len(supplied):
            _validation_error(info, 'MONTHS requires 1-12 unique months.')
        if any(period not in all_periods for period in supplied):
            _validation_error(info, 'A selected month falls outside the financial year.')
        selected_set = set(supplied)
        selected = [period for period in all_periods if period in selected_set]

    start_year = financial_year if start_month == 1 else financial_year - 1
    starts_on = datetime.date(start_year, start_month, 1)
    if start_month == 1:
        ends_on = datetime.date(financial_year, 12, 31)
    else:
        ends_on = datetime.date(financial_year, start_month, 1) - datetime.timedelta(days=1)

    resolved = ResolvedFinancialContext(
        entity_id=strawberry.ID(str(membership.entity_id)),
        financial_year=financial_year,
        fiscal_year_start_month=start_month,
        starts_on=starts_on,
        ends_on=ends_on,
        selection_mode=mode,
        selected_periods=[YearMonthValue(period) for period in selected],
    )
    return membership, resolved
