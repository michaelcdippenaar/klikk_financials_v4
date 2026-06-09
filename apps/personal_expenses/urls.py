from django.urls import path

from . import views

app_name = 'personal_expenses'

urlpatterns = [
    path('report/', views.report_view, name='report'),
    path('transactions/', views.classified_transaction_list_view, name='transaction_list'),
    path('transactions/<int:transaction_id>/classification/', views.override_view, name='override'),
    path('rules/', views.rules_list_create_view, name='rules'),
    path('rules/<int:rule_id>/', views.rule_detail_view, name='rule_detail'),
    path('categories/', views.category_list_view, name='categories'),
    path('classify/', views.classify_trigger_view, name='classify'),
]
