from django.urls import path
from . import views

app_name = 'investec'

urlpatterns = [
    path('upload/', views.excel_upload_view, name='excel_upload'),
    path('transactions/', views.transaction_list_view, name='transaction_list'),
    path('portfolio/upload/', views.portfolio_upload_view, name='portfolio_upload'),
    path('mapping/', views.mapping_list_view, name='mapping_list'),
    path('mapping/unmapped-share-names/', views.unmapped_share_names_view, name='unmapped_share_names'),
    path('mapping/upload/', views.mapping_upload_view, name='mapping_upload'),
    path('export/mapping/', views.export_mapping_view, name='export_mapping'),
    path('export/companies/', views.export_companies_view, name='export_companies'),
    path('export/share-names/', views.export_share_names_view, name='export_share_names'),
    path('export/transactions/', views.export_transactions_view, name='export_transactions'),
    path('bank/accounts/', views.bank_account_list_view, name='bank_account_list'),
    path('bank/transactions/', views.bank_transaction_list_view, name='bank_transaction_list'),
    path('bank/sync-status/', views.bank_sync_status_view, name='bank_sync_status'),
    path('bank/sync/', views.bank_sync_trigger_view, name='bank_sync_trigger'),
]

