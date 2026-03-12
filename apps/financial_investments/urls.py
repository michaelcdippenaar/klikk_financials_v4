from django.urls import path
from . import views

app_name = 'financial_investments'

urlpatterns = [
    path('symbols/', views.symbol_list),
    path('symbols/<str:symbol>/', views.symbol_detail),
    path('symbols/<str:symbol>/history/', views.symbol_history),
    path('symbols/<str:symbol>/refresh/', views.symbol_refresh),
    path('symbols/<str:symbol>/refresh-extra/', views.symbol_refresh_extra),
    path('symbols/<str:symbol>/dividends/', views.symbol_dividends),
    path('symbols/<str:symbol>/splits/', views.symbol_splits),
    path('symbols/<str:symbol>/info/', views.symbol_info),
    path('symbols/<str:symbol>/financial-statements/', views.symbol_financial_statements),
    path('symbols/<str:symbol>/earnings/', views.symbol_earnings),
    path('symbols/<str:symbol>/earnings-estimate/', views.symbol_earnings_estimate),
    path('symbols/<str:symbol>/analyst-recommendations/', views.symbol_analyst_recommendations),
    path('symbols/<str:symbol>/analyst-price-target/', views.symbol_analyst_price_target),
    path('symbols/<str:symbol>/ownership/', views.symbol_ownership),
    path('symbols/<str:symbol>/news/', views.symbol_news),
    path('watchlist-preference/save/', views.watchlist_preference_save),
    path('watchlist-preference/', views.watchlist_preference),
]
