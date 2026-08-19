from django.urls import path

from . import views

app_name = 'audit'

urlpatterns = [
    path('checks/', views.checks_view, name='checks'),
    path('checks/<str:code>/', views.check_detail_view, name='check_detail'),
    path('checks/<str:code>/run/', views.run_check_view, name='run_check'),
    path('run/', views.run_audit_view, name='run_audit'),
    path('history/<str:code>/', views.history_view, name='history'),
    path('runs/', views.runs_view, name='runs'),
]
