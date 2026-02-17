"""
Xero Validation Views Package

This package contains all API views for validation functionality.
Views are separated by domain:
- trial_balance: Views for Trial Balance report operations
- profit_loss: Views for Profit & Loss report operations
- common: Shared helper functions and base classes
"""

from .trial_balance_views import (
    ValidateBalanceSheetCompleteView,
    ImportTrailBalanceView,
    CompareTrailBalanceView,
    TrailBalanceComparisonDetailsView,
    ImportAndExportTrailBalanceView,
    ValidateBalanceSheetAccountsView,
    ExportLineItemsView,
    ExportTrailBalanceCompleteView,
    AddIncomeStatementToReportView,
)

from .profit_loss_views import (
    ImportProfitLossView,
    CompareProfitLossView,
    ExportProfitLossCompleteView,
)

from .reconciliation_views import ReconcileReportsView

__all__ = [
    # Trial Balance Views
    'ValidateBalanceSheetCompleteView',
    'ImportTrailBalanceView',
    'CompareTrailBalanceView',
    'TrailBalanceComparisonDetailsView',
    'ImportAndExportTrailBalanceView',
    'ValidateBalanceSheetAccountsView',
    'ExportLineItemsView',
    'ExportTrailBalanceCompleteView',
    'AddIncomeStatementToReportView',
    # Profit & Loss Views
    'ImportProfitLossView',
    'CompareProfitLossView',
    'ExportProfitLossCompleteView',
    # Reconciliation
    'ReconcileReportsView',
]

