from django.urls import path
from apps.xero.xero_auth import views

app_name = 'xero_auth'

urlpatterns = [
    path('initiate/', views.XeroAuthInitiateView.as_view(), name='xero-auth-initiate'),
    path('callback/', views.XeroCallbackView.as_view(), name='xero-callback'),
    path('status/', views.XeroConnectionStatusView.as_view(), name='xero-connection-status'),
    path('credentials/', views.XeroCredentialsView.as_view(), name='xero-credentials'),
]
