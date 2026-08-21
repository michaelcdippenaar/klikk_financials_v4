from django.urls import path

from .auth_views import LoginView, LogoutView, RefreshView, VerifyView


app_name = 'web_api_v2_auth'

urlpatterns = [
    path('login/', LoginView.as_view(), name='login'),
    path('refresh/', RefreshView.as_view(), name='refresh'),
    path('verify/', VerifyView.as_view(), name='verify'),
    path('logout/', LogoutView.as_view(), name='logout'),
]
