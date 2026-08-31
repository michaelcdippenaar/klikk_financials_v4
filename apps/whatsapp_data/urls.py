from django.urls import path

from . import views

app_name = 'whatsapp_data'

urlpatterns = [
    path('chats/', views.chats_view, name='chats'),
    path('messages/', views.messages_view, name='messages'),
    path('context/', views.context_view, name='context'),
    path('attachment/', views.attachment_view, name='attachment'),
]
