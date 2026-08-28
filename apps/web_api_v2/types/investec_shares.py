import datetime
from decimal import Decimal
from typing import List, Optional

import strawberry

from apps.web_api_v2.types.financial_context import ResolvedFinancialContext


@strawberry.type
class InvestecShareHolding:
    id: str
    share_code: str
    company: str
    quantity: Decimal
    currency: str
    total_cost: Decimal
    total_value: Decimal


@strawberry.type
class InvestecShareTransactionRow:
    id: str
    date: Optional[datetime.date]
    type: str
    share_name: str
    description: str
    quantity: Decimal
    value: Decimal


@strawberry.type
class InvestecShareSummary:
    transaction_count: int
    net_value: Optional[Decimal]
    earliest_date: Optional[datetime.date]
    latest_date: Optional[datetime.date]


@strawberry.type
class InvestecShareAccount:
    context: ResolvedFinancialContext
    available: bool
    user_safe_reason: Optional[str]
    # Last four digits only.
    account_masked: Optional[str]
    # Holdings are a position at a date, not a period aggregate: summing
    # snapshots across a period would multiply the portfolio.
    holdings_as_at: Optional[datetime.date]
    holdings_value: Optional[Decimal]
    holdings: List[InvestecShareHolding]
    summary: Optional[InvestecShareSummary]
    transaction_count: int
    transactions: List[InvestecShareTransactionRow]
    # Share names in this period that no mapping resolves to a share code.
    # This account is loaded by upload and mapping, so an unmapped name is a
    # real gap: its transactions cannot be attributed to a holding.
    unmapped_share_names: List[str]
