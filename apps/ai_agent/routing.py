from django.urls import path
from .consumers import ChatObserverConsumer

websocket_urlpatterns = [
    path('ws/ai-agent/chat/', ChatObserverConsumer.as_asgi()),
    path('ws/ai-agent/chat/<session_id>/', ChatObserverConsumer.as_asgi()),
]
