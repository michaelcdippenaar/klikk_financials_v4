"""
User authentication URLs.
"""
from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView, TokenVerifyView
from .views import RegisterView, LoginView, RefreshTokenView, NginxAuthCheckView

app_name = 'user'

urlpatterns = [
    # Custom views
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),
    path('refresh/', RefreshTokenView.as_view(), name='refresh'),
    
    # SimpleJWT built-in views (alternative endpoints)
    path('token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('token/verify/', TokenVerifyView.as_view(), name='token_verify'),
    path('nginx-check/', NginxAuthCheckView.as_view(), name='nginx_auth_check'),
]
