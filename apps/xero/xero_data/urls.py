from django.urls import path, re_path
from apps.xero.xero_data import views

app_name = 'xero_data'

urlpatterns = [
    path('journals/search/', views.XeroJournalSearchView.as_view(), name='journal_search'),
    path('update/journals/', views.XeroUpdateDataView.as_view(), name='update_data'),
    # Support both with and without trailing slash
    path('process/journals/', views.XeroProcessJournalsView.as_view(), name='process_journals'),
    re_path(r'^process/journals$', views.XeroProcessJournalsView.as_view(), name='process_journals_no_slash'),
    path('sync/documents/', views.XeroSyncDocumentsView.as_view(), name='sync_documents'),
    path('documents/by-transaction/<str:transaction_id>/', views.XeroDocumentsByTransactionView.as_view(), name='documents_by_transaction'),

    # Aged reports — sync triggers
    path('aged-payables/sync/', views.XeroSyncAgedPayablesView.as_view(), name='sync_aged_payables'),
    path('aged-receivables/sync/', views.XeroSyncAgedReceivablesView.as_view(), name='sync_aged_receivables'),

    # Aged reports — list views (future UI)
    path('aged-payables/', views.XeroAgedPayablesListView.as_view(), name='aged_payables_list'),
    path('aged-receivables/', views.XeroAgedReceivablesListView.as_view(), name='aged_receivables_list'),
]
