"""
User authentication views - Registration and Login with JWT tokens.
"""
from rest_framework import status, generics
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken, AccessToken
from rest_framework_simplejwt.exceptions import TokenError
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import HttpResponse

User = get_user_model()


class RegisterView(generics.CreateAPIView):
    """
    User registration endpoint.
    
    Creates a new user account and returns JWT tokens.
    
    POST /api/auth/register/
    {
        "username": "user123",
        "email": "user@example.com",
        "password": "securepassword123",
        "password_confirm": "securepassword123",
        "first_name": "John",  # Optional
        "last_name": "Doe"     # Optional
    }
    """
    permission_classes = [AllowAny]
    queryset = User.objects.all()
    
    def post(self, request, *args, **kwargs):
        username = request.data.get('username')
        email = request.data.get('email')
        password = request.data.get('password')
        password_confirm = request.data.get('password_confirm')
        first_name = request.data.get('first_name', '')
        last_name = request.data.get('last_name', '')
        
        # Validation
        if not username:
            return Response(
                {"error": "Username is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not email:
            return Response(
                {"error": "Email is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not password:
            return Response(
                {"error": "Password is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if password != password_confirm:
            return Response(
                {"error": "Passwords do not match"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate password strength
        try:
            validate_password(password)
        except ValidationError as e:
            return Response(
                {"error": "Password validation failed", "details": list(e.messages)},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if user already exists
        if User.objects.filter(username=username).exists():
            return Response(
                {"error": "Username already exists"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if User.objects.filter(email=email).exists():
            return Response(
                {"error": "Email already exists"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create user
        try:
            # Note: Users created via API are regular users (not staff) by default
            # Use management command: python manage.py promote_user <username> --staff
            # to grant admin access
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
                is_active=True,
                is_staff=False,  # Must be set explicitly via admin or management command
                is_superuser=False  # Must be set explicitly via admin or management command
            )
            
            # Generate JWT tokens
            refresh = RefreshToken.for_user(user)
            
            return Response({
                "message": "User registered successfully",
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                },
                "tokens": {
                    "refresh": str(refresh),
                    "access": str(refresh.access_token),
                }
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            return Response(
                {"error": f"Failed to create user: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class LoginView(TokenObtainPairView):
    """
    User login endpoint.
    
    Authenticates user credentials and returns JWT tokens.
    
    POST /api/auth/login/
    {
        "username": "user123",  # or "email": "user@example.com"
        "password": "securepassword123"
    }
    
    Note: You can use either username or email to login.
    """
    permission_classes = [AllowAny]
    
    def post(self, request, *args, **kwargs):
        username_or_email = request.data.get('username') or request.data.get('email')
        password = request.data.get('password')
        
        if not username_or_email:
            return Response(
                {"error": "Username or email is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not password:
            return Response(
                {"error": "Password is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Try to find user by username or email
        try:
            if '@' in username_or_email:
                user = User.objects.get(email=username_or_email)
            else:
                user = User.objects.get(username=username_or_email)
        except User.DoesNotExist:
            return Response(
                {"error": "Invalid credentials"},
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        # Check password
        if not user.check_password(password):
            return Response(
                {"error": "Invalid credentials"},
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        # Check if user is active
        if not user.is_active:
            return Response(
                {"error": "User account is inactive"},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        
        return Response({
            "message": "Login successful",
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
            "tokens": {
                "refresh": str(refresh),
                "access": str(refresh.access_token),
            }
        }, status=status.HTTP_200_OK)


class NginxAuthCheckView(APIView):
    """
    Lightweight endpoint for nginx auth_request.
    Reads JWT from the klikk_token cookie and verifies it.
    Returns 200 if valid, 401 if missing/invalid.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        token = request.COOKIES.get('klikk_token')
        if not token:
            return HttpResponse(status=401)
        try:
            AccessToken(token)
            return HttpResponse(status=200)
        except TokenError:
            return HttpResponse(status=401)


class RefreshTokenView(generics.GenericAPIView):
    """
    Refresh JWT access token endpoint.
    
    POST /api/auth/refresh/
    {
        "refresh": "your_refresh_token_here"
    }
    """
    permission_classes = [AllowAny]
    
    def post(self, request, *args, **kwargs):
        refresh_token = request.data.get('refresh')
        
        if not refresh_token:
            return Response(
                {"error": "Refresh token is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            refresh = RefreshToken(refresh_token)
            access_token = refresh.access_token
            
            return Response({
                "access": str(access_token),
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            return Response(
                {"error": "Invalid refresh token"},
                status=status.HTTP_401_UNAUTHORIZED
            )
