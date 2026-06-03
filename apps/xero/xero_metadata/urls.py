from django.urls import path
from apps.xero.xero_metadata import views

app_name = 'xero_metadata'

urlpatterns = [
    path('update/', views.XeroUpdateMetadataView.as_view(), name='update_metadata'),
    path('accounts/search/', views.account_search, name='account_search'),
    # List endpoints for MCP / agent consumption (2026-06-03)
    path('contacts/', views.XeroContactListView.as_view(), name='contacts_list'),
    path('tracking/', views.XeroTrackingListView.as_view(), name='tracking_list'),
    path('accounts/', views.XeroAccountListView.as_view(), name='accounts_list'),
]
