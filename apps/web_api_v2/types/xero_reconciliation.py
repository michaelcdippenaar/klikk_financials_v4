import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

import strawberry

from apps.web_api_v2.types.financial_context import ResolvedFinancialContext


@strawberry.enum
class ReconciliationAccountClass(Enum):
    ASSET = 'ASSET'
    LIABILITY = 'LIABILITY'
    EQUITY = 'EQUITY'
    REVENUE = 'REVENUE'
    EXPENSE = 'EXPENSE'


@strawberry.enum
class ReconciliationBasis(Enum):
    """Which span of ledger history Xero's reported figure is met with.

    Profit-and-loss accounts reset each fiscal year; balance-sheet accounts
    carry forward from inception. Using one basis for both is what made the
    previous comparison unusable.
    """

    FISCAL_YEAR_TO_DATE = 'FISCAL_YEAR_TO_DATE'
    INCEPTION_TO_DATE = 'INCEPTION_TO_DATE'


@strawberry.enum
class ReconciliationStatus(Enum):
    MATCHED = 'MATCHED'
    VARIANCE = 'VARIANCE'
    MISSING_IN_LEDGER = 'MISSING_IN_LEDGER'
    MISSING_IN_XERO = 'MISSING_IN_XERO'
    UNCLASSIFIED = 'UNCLASSIFIED'


@strawberry.type
class ReconciliationRow:
    account_id: Optional[str]
    account_code: str
    account_name: str
    account_class: Optional[ReconciliationAccountClass]
    # Xero's own statement grouping and reporting line, so the P&L and
    # balance-sheet views group the way the statements do.
    reporting_group: Optional[str]
    reporting_line: Optional[str]
    # Null wherever the account could not be classified: the row is reported
    # without a comparison rather than compared on a guessed basis.
    basis: Optional[ReconciliationBasis]
    xero_value: Optional[Decimal]
    ledger_value: Optional[Decimal]
    variance: Optional[Decimal]
    status: ReconciliationStatus
    # The server's own words for why this row is not a plain comparison.
    user_safe_reason: Optional[str]


@strawberry.type
class ReconciliationSummary:
    accounts_compared: int
    reconciled: int
    needs_attention: int
    net_variance: Decimal
    # Counts per status, so a screen can separate genuine disagreements from
    # structural asymmetries instead of presenting one undifferentiated total.
    variance_count: int
    missing_in_ledger_count: int
    missing_in_xero_count: int
    unclassified_count: int


@strawberry.type
class XeroReconciliation:
    context: ResolvedFinancialContext
    available: bool
    # Populated only when available is False, and never a generic word.
    user_safe_reason: Optional[str]
    report_date: Optional[datetime.date]
    imported_at: Optional[datetime.datetime]
    fiscal_year_start_month: Optional[int]
    tolerance: Optional[Decimal]
    summary: Optional[ReconciliationSummary]
    rows: List[ReconciliationRow]


@strawberry.type
class ReconciliationAccountLine:
    id: str
    date: Optional[datetime.date]
    reference: str
    description: str
    source: str
    ledger_value: Optional[Decimal]
    # Deliberately absent: Xero's trial balance is an account-level report and
    # publishes no line-level figure, so there is nothing truthful to compare
    # a single ledger line against.


@strawberry.type
class ReconciliationAccountDetail:
    account_id: str
    account_code: str
    account_name: str
    reporting_group: Optional[str]
    reporting_line: Optional[str]
    truncated: bool
    limit: int
    lines: List[ReconciliationAccountLine]
    # Says why no Xero column appears beside these lines.
    comparison_note: str
