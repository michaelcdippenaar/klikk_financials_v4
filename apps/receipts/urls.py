from django.urls import path

from . import views

app_name = 'receipts'

urlpatterns = [
    path('', views.receipts_list_view, name='list'),
    path('export/', views.receipts_export_view, name='export'),
    path('<str:sha256>/', views.receipt_detail_view, name='detail'),
    path('<str:sha256>/review/', views.receipt_review_view, name='review'),
    path('<str:sha256>/comments/', views.receipt_comments_view, name='comments'),
]
