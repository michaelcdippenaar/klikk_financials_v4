from django.urls import path, re_path
from apps.xero.xero_data import views

app_name = 'xero_data'

urlpatterns = [
    path('update/journals/', views.XeroUpdateDataView.as_view(), name='update_data'),
    # Support both with and without trailing slash
    path('process/journals/', views.XeroProcessJournalsView.as_view(), name='process_journals'),
    re_path(r'^process/journals$', views.XeroProcessJournalsView.as_view(), name='process_journals_no_slash'),
    path('sync/documents/', views.XeroSyncDocumentsView.as_view(), name='sync_documents'),
    path('documents/by-transaction/<str:transaction_id>/', views.XeroDocumentsByTransactionView.as_view(), name='documents_by_transaction'),
]

