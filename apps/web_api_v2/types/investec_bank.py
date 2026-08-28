import datetime
from decimal import Decimal
from typing import List, Optional

import strawberry

from apps.web_api_v2.types.financial_context import ResolvedFinancialContext


@strawberry.type
class InvestecBankAccountRef:
    id: str
    # Last four digits only: enough to tell accounts apart, and a full bank
    # account number on a page is a liability with no matching benefit.
    masked_number: str
    name: str


@strawberry.type
class InvestecBankTransactionRow:
    id: str
    date: Optional[datetime.date]
    account_masked: str
    description: str
    transaction_type: str
    status: str
    amount: Decimal
    running_balance: Optional[Decimal]
    # There is deliberately no validation or evidence field. No V2 contract
    # checks a bank transaction against anything, and an empty column would
    # read as "checked, nothing found".


@strawberry.type
class InvestecBankSummary:
    transaction_count: int
    net_amount: Optional[Decimal]
    earliest_date: Optional[datetime.date]
    latest_date: Optional[datetime.date]
    freshness_at: Optional[datetime.datetime]


@strawberry.type
class InvestecBankTransactions:
    context: ResolvedFinancialContext
    available: bool
    user_safe_reason: Optional[str]
    accounts: List[InvestecBankAccountRef]
    summary: Optional[InvestecBankSummary]
    total_count: int
    rows: List[InvestecBankTransactionRow]
