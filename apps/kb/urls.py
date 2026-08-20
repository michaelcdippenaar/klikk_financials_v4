from django.urls import path

from . import views

urlpatterns = [
    path('documents/', views.list_documents, name='kb-documents'),
    path('documents/<slug:slug>/', views.read_document, name='kb-document'),
    path('search/', views.search, name='kb-search'),
    path('suppliers/', views.suppliers, name='kb-suppliers'),
    path('customers/', views.customers, name='kb-customers'),
    path('accounts/', views.accounts, name='kb-accounts'),
    path('tracking/', views.tracking, name='kb-tracking'),
    path('events/', views.events, name='kb-events'),
]
