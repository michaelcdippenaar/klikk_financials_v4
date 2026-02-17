from django.urls import path
from .views import (
    ValidateBalanceSheetCompleteView,
    ImportProfitLossView,
    CompareProfitLossView,
    ExportTrailBalanceCompleteView,
    ExportProfitLossCompleteView,
    ReconcileReportsView,
)

app_name = 'xero_validation'

urlpatterns = [
    # Combined validation endpoint (can run all steps or individual steps)
    path('balance-sheet/', ValidateBalanceSheetCompleteView.as_view(), name='validate_complete'),
    
    # Reconciliation: P&L + Balance Sheet vs trail balance, per financial year
    path('reconcile/', ReconcileReportsView.as_view(), name='reconcile_reports'),
    
    # Profit and Loss endpoints
    path('import-profit-loss/', ImportProfitLossView.as_view(), name='import_profit_loss'),
    path('compare-profit-loss/', CompareProfitLossView.as_view(), name='compare_profit_loss'),
    
    # Export endpoints
    path('export-trail-balance/', ExportTrailBalanceCompleteView.as_view(), name='export_trail_balance_complete'),
    path('export-profit-loss/', ExportProfitLossCompleteView.as_view(), name='export_profit_loss_complete'),
    
    # Individual endpoints (for backward compatibility)
    # path('import-trail-balance/', views.ImportTrailBalanceView.as_view(), name='import_trail_balance'),
    # path('import-export-trail-balance/', views.ImportAndExportTrailBalanceView.as_view(), name='import_export_trail_balance'),
    # path('compare-trail-balance/', views.CompareTrailBalanceView.as_view(), name='compare_trail_balance'),
    # path('validate-balance-sheet/', views.ValidateBalanceSheetAccountsView.as_view(), name='validate_balance_sheet'),
    # path('export-line-items/', views.ExportLineItemsView.as_view(), name='export_line_items'),
    # path('add-income-statement/', views.AddIncomeStatementToReportView.as_view(), name='add_income_statement'),
    # path('comparison-details/<int:report_id>/', views.TrailBalanceComparisonDetailsView.as_view(), name='comparison_details'),
]
