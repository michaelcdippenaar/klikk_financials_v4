from django.urls import path
from apps.xero.xero_cube import views

app_name = 'xero_cube'

urlpatterns = [
    path('process/', views.XeroProcessDataView.as_view(), name='xero-process-data'),
    path('summary/', views.XeroDataSummaryView.as_view(), name='xero-data-summary'),
    path('trail-balance/', views.XeroTrailBalanceListView.as_view(), name='xero-trail-balance-list'),
    path('line-items/', views.XeroLineItemsListView.as_view(), name='xero-line-items-list'),
    path('import-pnl-by-tracking/', views.ImportPnlByTrackingView.as_view(), name='xero-import-pnl-by-tracking'),
    path('pnl-summary/', views.PnlSummaryByTrackingView.as_view(), name='xero-pnl-summary'),
    path('account-summary/', views.AccountBalanceSummaryView.as_view(), name='xero-account-summary'),
]
