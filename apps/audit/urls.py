from django.urls import path

from apps.audit import views

app_name = 'audit'

urlpatterns = [
    path('checks/', views.AuditChecksView.as_view(), name='audit-checks'),
]
