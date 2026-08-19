from django.urls import path

from . import views

app_name = 'pricelist'

urlpatterns = [
    path('items/', views.items_view, name='items'),
    path('items/<str:code>/', views.item_detail_view, name='item_detail'),
    path('items/<str:code>/price/', views.item_price_view, name='item_price'),
    path('items/<str:code>/prices/', views.item_prices_view, name='item_prices'),
    path('quote/', views.quote_view, name='quote'),
    path('export/', views.export_view, name='export'),
]
