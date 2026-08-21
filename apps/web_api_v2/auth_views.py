import logging
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.db import DatabaseError
from rest_framework import status
from rest_framework.exceptions import APIException, AuthenticationFailed, NotAuthenticated, ParseError, Throttled
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer, TokenVerifySerializer
from rest_framework_simplejwt.tokens import RefreshToken


logger = logging.getLogger(__name__)
User = get_user_model()
DUMMY_PASSWORD_HASH = make_password('web-api-v2-invalid-user-password')


class RequestTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = 'Request body is too large.'
    default_code = 'request_too_large'


def _payload(request):
    return request.data if isinstance(request.data, dict) else {}


def _correlation_id(request):
    correlation_id = getattr(request, 'web_api_v2_correlation_id', None)
    if correlation_id is None:
        correlation_id = str(uuid.uuid4())
        request.web_api_v2_correlation_id = correlation_id
    return correlation_id


def _response(request, payload, http_status):
    response = Response(payload, status=http_status)
    response['X-Correlation-ID'] = _correlation_id(request)
    return response


def _error(request, code, message, http_status, *, retryable=False):
    return _response(
        request,
        {
            'error': {
                'code': code,
                'message': message,
                'correlationId': _correlation_id(request),
                'retryable': retryable,
            },
        },
        http_status,
    )


def _log(request, operation, outcome, *, user_id=None):
    logger.info(
        'v2_auth operation=%s outcome=%s user=%s correlation_id=%s',
        operation,
        outcome,
        user_id or '-',
        _correlation_id(request),
    )


class TypedAuthView(APIView):
    def initial(self, request, *args, **kwargs):
        content_length = request.META.get('CONTENT_LENGTH')
        try:
            if content_length and int(content_length) > settings.WEB_API_V2_AUTH_MAX_REQUEST_BYTES:
                raise RequestTooLarge
        except ValueError:
            raise ParseError('Invalid Content-Length header.') from None
        return super().initial(request, *args, **kwargs)

    def handle_exception(self, exc):
        if isinstance(exc, (AuthenticationFailed, NotAuthenticated)):
            return _error(
                self.request, 'UNAUTHENTICATED', 'Authentication credentials were not accepted.',
                status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(exc, Throttled):
            return _error(
                self.request, 'RATE_LIMITED', 'Too many authentication requests.',
                status.HTTP_429_TOO_MANY_REQUESTS, retryable=True,
            )
        if isinstance(exc, ParseError):
            return _error(
                self.request, 'VALIDATION_ERROR', 'Request body is not valid JSON.',
                status.HTTP_400_BAD_REQUEST,
            )
        if isinstance(exc, RequestTooLarge):
            return _error(
                self.request, 'VALIDATION_ERROR', 'Authentication request is too large.',
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        return super().handle_exception(exc)


class PublicAuthView(TypedAuthView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)


class BrowserJWTView(TypedAuthView):
    authentication_classes = (JWTAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (ScopedRateThrottle,)


class LoginView(PublicAuthView):
    """Obtain a browser SimpleJWT access/refresh pair."""

    throttle_scope = 'v2_auth_login'

    def post(self, request):
        payload = _payload(request)
        identifier = payload.get('username') or payload.get('email')
        password = payload.get('password')
        if not isinstance(identifier, str) or not identifier.strip() or not isinstance(password, str):
            _log(request, 'login', 'validation_error')
            return _error(
                request, 'VALIDATION_ERROR', 'Username/email and password are required.',
                status.HTTP_400_BAD_REQUEST,
            )

        identifier = identifier.strip()
        try:
            if '@' in identifier:
                user = (
                    User.objects.filter(username=identifier).first()
                    or User.objects.filter(email=identifier).order_by('pk').first()
                )
            else:
                user = (
                    User.objects.filter(username=identifier).order_by('pk').first()
                    or User.objects.filter(email=identifier).order_by('pk').first()
                )
        except DatabaseError:
            logger.error(
                'v2_auth operation=login outcome=unavailable correlation_id=%s',
                _correlation_id(request),
            )
            return _error(
                request, 'TEMPORARILY_UNAVAILABLE', 'Authentication is temporarily unavailable.',
                status.HTTP_503_SERVICE_UNAVAILABLE, retryable=True,
            )

        password_valid = (
            user.check_password(password)
            if user is not None
            else check_password(password, DUMMY_PASSWORD_HASH)
        )
        if user is None or not password_valid:
            _log(request, 'login', 'invalid_credentials')
            return _error(
                request, 'INVALID_CREDENTIALS', 'Authentication credentials were not accepted.',
                status.HTTP_401_UNAUTHORIZED,
            )
        if not user.is_active:
            _log(request, 'login', 'inactive', user_id=user.pk)
            return _error(
                request, 'ACCOUNT_INACTIVE', 'This user account is inactive.',
                status.HTTP_403_FORBIDDEN,
            )

        refresh = RefreshToken.for_user(user)
        _log(request, 'login', 'succeeded', user_id=user.pk)
        return _response(
            request,
            {
                'user': {
                    'id': str(user.pk),
                    'username': user.username,
                    'email': user.email,
                    'firstName': user.first_name,
                    'lastName': user.last_name,
                },
                'tokens': {'access': str(refresh.access_token), 'refresh': str(refresh)},
            },
            status.HTTP_200_OK,
        )


class RefreshView(PublicAuthView):
    """Rotate a browser refresh token using the project SimpleJWT policy."""

    throttle_scope = 'v2_auth_refresh'

    def post(self, request):
        raw_refresh = _payload(request).get('refresh')
        if not isinstance(raw_refresh, str) or not raw_refresh:
            _log(request, 'refresh', 'validation_error')
            return _error(
                request, 'VALIDATION_ERROR', 'Refresh token is required.',
                status.HTTP_400_BAD_REQUEST,
            )
        serializer = TokenRefreshSerializer(data={'refresh': raw_refresh})
        try:
            serializer.is_valid(raise_exception=True)
        except DatabaseError:
            logger.error(
                'v2_auth operation=refresh outcome=unavailable correlation_id=%s',
                _correlation_id(request),
            )
            return _error(
                request, 'TEMPORARILY_UNAVAILABLE', 'Authentication is temporarily unavailable.',
                status.HTTP_503_SERVICE_UNAVAILABLE, retryable=True,
            )
        except TokenError:
            _log(request, 'refresh', 'invalid_token')
            return _error(
                request, 'TOKEN_INVALID', 'Refresh token was not accepted.',
                status.HTTP_401_UNAUTHORIZED,
            )
        except Exception:
            _log(request, 'refresh', 'invalid_token')
            return _error(
                request, 'TOKEN_INVALID', 'Refresh token was not accepted.',
                status.HTTP_401_UNAUTHORIZED,
            )
        _log(request, 'refresh', 'succeeded')
        return _response(request, serializer.validated_data, status.HTTP_200_OK)


class VerifyView(PublicAuthView):
    """Verify a browser JWT without accepting project service credentials."""

    throttle_scope = 'v2_auth_verify'

    def post(self, request):
        raw_token = _payload(request).get('token')
        if not isinstance(raw_token, str) or not raw_token:
            _log(request, 'verify', 'validation_error')
            return _error(
                request, 'VALIDATION_ERROR', 'Token is required.',
                status.HTTP_400_BAD_REQUEST,
            )
        serializer = TokenVerifySerializer(data={'token': raw_token})
        try:
            serializer.is_valid(raise_exception=True)
        except DatabaseError:
            logger.error(
                'v2_auth operation=verify outcome=unavailable correlation_id=%s',
                _correlation_id(request),
            )
            return _error(
                request, 'TEMPORARILY_UNAVAILABLE', 'Authentication is temporarily unavailable.',
                status.HTTP_503_SERVICE_UNAVAILABLE, retryable=True,
            )
        except Exception:
            _log(request, 'verify', 'invalid_token')
            return _error(
                request, 'TOKEN_INVALID', 'Token was not accepted.',
                status.HTTP_401_UNAUTHORIZED,
            )
        _log(request, 'verify', 'succeeded')
        return _response(request, {'valid': True}, status.HTTP_200_OK)


class LogoutView(BrowserJWTView):
    """Revoke a refresh token belonging to the authenticated browser user."""

    throttle_scope = 'v2_auth_logout'

    def post(self, request):
        raw_refresh = _payload(request).get('refresh')
        if not isinstance(raw_refresh, str) or not raw_refresh:
            _log(request, 'logout', 'validation_error', user_id=request.user.pk)
            return _error(
                request, 'VALIDATION_ERROR', 'Refresh token is required.',
                status.HTTP_400_BAD_REQUEST,
            )
        try:
            refresh = RefreshToken(raw_refresh)
            if str(refresh.get('user_id')) != str(request.user.pk):
                _log(request, 'logout', 'subject_mismatch', user_id=request.user.pk)
                return _error(
                    request, 'TOKEN_SUBJECT_MISMATCH', 'Refresh token does not belong to this user.',
                    status.HTTP_403_FORBIDDEN,
                )
            if not hasattr(refresh, 'blacklist'):
                logger.error(
                    'v2_auth operation=logout outcome=revocation_unavailable user=%s '
                    'correlation_id=%s',
                    request.user.pk,
                    _correlation_id(request),
                )
                return _error(
                    request, 'TEMPORARILY_UNAVAILABLE', 'Logout is temporarily unavailable.',
                    status.HTTP_503_SERVICE_UNAVAILABLE, retryable=True,
                )
            refresh.blacklist()
        except TokenError:
            _log(request, 'logout', 'invalid_token', user_id=request.user.pk)
            return _error(
                request, 'TOKEN_INVALID', 'Refresh token was not accepted.',
                status.HTTP_400_BAD_REQUEST,
            )
        except DatabaseError:
            logger.error(
                'v2_auth operation=logout outcome=unavailable user=%s correlation_id=%s',
                request.user.pk,
                _correlation_id(request),
            )
            return _error(
                request, 'TEMPORARILY_UNAVAILABLE', 'Logout is temporarily unavailable.',
                status.HTTP_503_SERVICE_UNAVAILABLE, retryable=True,
            )
        _log(request, 'logout', 'succeeded', user_id=request.user.pk)
        return _response(
            request,
            {
                'refreshTokenRevoked': True,
                'accessTokenRevoked': False,
                'accessTokenValidUntilExpiry': True,
            },
            status.HTTP_200_OK,
        )
