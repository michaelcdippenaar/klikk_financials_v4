from django.urls import path, re_path
from apps.xero.xero_data import views, pivot_views, pivot_comments, cube_saved, cube_mentions, document_views

app_name = 'xero_data'

urlpatterns = [
    path('journals/search/', views.XeroJournalSearchView.as_view(), name='journal_search'),
    path('journals/filters/', views.XeroJournalFilterOptionsView.as_view(), name='journal_filters'),
    path('journals/pivot/', pivot_views.XeroJournalPivotView.as_view(), name='journal_pivot'),
    path('journals/pivot/dimensions/', pivot_views.XeroJournalPivotDimensionsView.as_view(), name='journal_pivot_dims'),
    path('journals/pivot/members/', pivot_views.XeroCubeMembersView.as_view(), name='journal_pivot_members'),
    path('journals/pivot/drill/', pivot_views.XeroCubeDrillView.as_view(), name='journal_pivot_drill'),
    path('journals/pivot/subsets/', cube_saved.XeroCubeSubsetsView.as_view(), name='journal_pivot_subsets'),
    path('journals/pivot/views/', cube_saved.XeroCubeViewsView.as_view(), name='journal_pivot_views'),
    path('comments/', pivot_comments.CommentsView.as_view(), name='comments'),
    path('journals/pivot/comments/', pivot_comments.XeroCubeCommentsView.as_view(), name='cube_comments'),
    path('journals/pivot/comments/bulk/', pivot_comments.XeroCubeCommentsBulkView.as_view(), name='cube_comments_bulk'),
    path('journals/pivot/comments/identity/', pivot_comments.XeroCubeCommentIdentityView.as_view(), name='cube_comment_identity'),
    path('journals/pivot/comments/<int:comment_id>/status/', pivot_comments.XeroCubeCommentStatusView.as_view(), name='cube_comment_status'),
    path('journals/pivot/people/', cube_mentions.XeroCubePeopleView.as_view(), name='cube_people'),
    path('update/journals/', views.XeroUpdateDataView.as_view(), name='update_data'),
    # Support both with and without trailing slash
    path('process/journals/', views.XeroProcessJournalsView.as_view(), name='process_journals'),
    re_path(r'^process/journals$', views.XeroProcessJournalsView.as_view(), name='process_journals_no_slash'),
    path('sync/documents/', views.XeroSyncDocumentsView.as_view(), name='sync_documents'),
    path('documents/search/', document_views.XeroDocumentSearchView.as_view(), name='document_search'),
    path('documents/<int:document_id>/file/', document_views.xero_document_file_view, name='document_file'),
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
    # create-draft/ must precede the '<str:invoice_id>/' catch-all
    path('invoices/create-draft/', views.XeroCreateDraftInvoiceView.as_view(), name='create_draft_invoice'),
    path('invoices/', views.XeroInvoiceListView.as_view(), name='invoices_list'),
    path('invoices/<str:invoice_id>/', views.XeroInvoiceDetailView.as_view(), name='invoice_detail'),
]
