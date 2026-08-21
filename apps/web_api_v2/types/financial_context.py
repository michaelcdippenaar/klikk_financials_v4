import datetime
from enum import Enum
from typing import Optional

import strawberry

from apps.web_api_v2.types.ingest import YearMonth


@strawberry.enum
class FinancialPeriodSelectionMode(Enum):
    MONTHS = 'MONTHS'
    ALL = 'ALL'


@strawberry.input
class FinancialPeriodSelectionInput:
    mode: FinancialPeriodSelectionMode
    months: Optional[list[YearMonth]] = None


@strawberry.input
class FinancialContextInput:
    entity_id: strawberry.ID
    financial_year: int
    period_selection: FinancialPeriodSelectionInput


@strawberry.type
class ResolvedFinancialContext:
    entity_id: strawberry.ID
    financial_year: int
    fiscal_year_start_month: int
    starts_on: datetime.date
    ends_on: datetime.date
    selection_mode: FinancialPeriodSelectionMode
    selected_periods: list[YearMonth]
