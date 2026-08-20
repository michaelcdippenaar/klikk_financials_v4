from django.urls import path, re_path
from apps.xero.xero_data import views, pivot_views, pivot_comments

app_name = 'xero_data'

urlpatterns = [
    path('journals/search/', views.XeroJournalSearchView.as_view(), name='journal_search'),
    path('journals/filters/', views.XeroJournalFilterOptionsView.as_view(), name='journal_filters'),
    path('journals/pivot/', pivot_views.XeroJournalPivotView.as_view(), name='journal_pivot'),
    path('journals/pivot/dimensions/', pivot_views.XeroJournalPivotDimensionsView.as_view(), name='journal_pivot_dims'),
    path('journals/pivot/members/', pivot_views.XeroCubeMembersView.as_view(), name='journal_pivot_members'),
    path('journals/pivot/comments/', pivot_comments.XeroCubeCommentsView.as_view(), name='cube_comments'),
    path('journals/pivot/comments/<int:comment_id>/status/', pivot_comments.XeroCubeCommentStatusView.as_view(), name='cube_comment_status'),
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

    # Quotes — sync + list + detail
    path('quotes/sync/', views.XeroSyncQuotesView.as_view(), name='sync_quotes'),
    path('quotes/', views.XeroQuoteListView.as_view(), name='quotes_list'),
    path('quotes/<str:quote_id>/', views.XeroQuoteDetailView.as_view(), name='quote_detail'),
    # Invoices — sync + list + detail (parallel to XeroTransactionSource)
    path('invoices/sync/', views.XeroSyncInvoicesView.as_view(), name='sync_invoices'),
    path('invoices/', views.XeroInvoiceListView.as_view(), name='invoices_list'),
    path('invoices/<str:invoice_id>/', views.XeroInvoiceDetailView.as_view(), name='invoice_detail'),
]
