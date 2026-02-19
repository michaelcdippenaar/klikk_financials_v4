from django.urls import path
from apps.xero.xero_sync import views

app_name = 'xero_sync'

urlpatterns = [
    path('update/', views.XeroUpdateModelsView.as_view(), name='xero-update-models'),
    path('api-call-stats/', views.XeroApiCallStatsView.as_view(), name='xero-api-call-stats'),
]
