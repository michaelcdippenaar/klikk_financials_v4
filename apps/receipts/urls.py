from django.urls import path

from . import views

app_name = 'receipts'

urlpatterns = [
    path('', views.receipts_list_view, name='list'),
    # export/ and bulk/ MUST precede '<str:sha256>/' — Django resolves in order and the catch-all
    # would otherwise swallow them as a sha256.
    path('export/', views.receipts_export_view, name='export'),
    path('bulk/', views.receipts_bulk_view, name='bulk'),
    path('<str:sha256>/', views.receipt_detail_view, name='detail'),
    path('<str:sha256>/review/', views.receipt_review_view, name='review'),
    path('<str:sha256>/comments/', views.receipt_comments_view, name='comments'),
]
