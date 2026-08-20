import json
import logging
import re
import time
import uuid

from django.conf import settings
from django.http import HttpResponseNotAllowed
from strawberry.django.views import GraphQLView

from .auth import (
    BrowserAuthenticationUnavailable,
    authenticate_browser_request,
)
from .errors import graphql_error_response


logger = logging.getLogger(__name__)


class AuthenticatedGraphQLView(GraphQLView):
    """JWT-only GraphQL transport for browser access.

    Service credentials and legacy DRF tokens are deliberately rejected.
    OPTIONS remains available for CORS preflight; GraphQL operations are POST-only.
    """

    http_method_names = ('post', 'options')

    def dispatch(self, request, *args, **kwargs):
        if request.method == 'OPTIONS':
            return super().dispatch(request, *args, **kwargs)
        if request.method != 'POST':
            return HttpResponseNotAllowed(('POST', 'OPTIONS'))

        correlation_id = str(uuid.uuid4())
        request.graphql_correlation_id = correlation_id
        content_length = request.META.get('CONTENT_LENGTH')
        if content_length:
            try:
                if int(content_length) > settings.WEB_API_V2_MAX_REQUEST_BYTES:
                    return graphql_error_response(
                        'GraphQL request is too large.', 'VALIDATION_ERROR', 413, correlation_id,
                    )
            except ValueError:
                return graphql_error_response(
                    'Invalid Content-Length header.', 'VALIDATION_ERROR', 400, correlation_id,
                )

        try:
            authenticated = authenticate_browser_request(request)
        except BrowserAuthenticationUnavailable:
            logger.error(
                'graphql_authentication_unavailable correlation_id=%s', correlation_id,
            )
            return graphql_error_response(
                'Authentication is temporarily unavailable.',
                'TEMPORARILY_UNAVAILABLE',
                503,
                correlation_id,
            )
        if not authenticated:
            return graphql_error_response(
                'Authentication credentials were not accepted.',
                'UNAUTHENTICATED',
                401,
                correlation_id,
            )

        if len(request.body) > settings.WEB_API_V2_MAX_REQUEST_BYTES:
            return graphql_error_response(
                'GraphQL request is too large.', 'VALIDATION_ERROR', 413, correlation_id,
            )

        operation_name = None
        try:
            payload = json.loads(request.body or b'{}')
            candidate = payload.get('operationName') if isinstance(payload, dict) else None
            if (
                isinstance(candidate, str)
                and len(candidate) <= 128
                and re.fullmatch(r'[_A-Za-z][_0-9A-Za-z]*', candidate)
            ):
                operation_name = candidate
        except (TypeError, ValueError, UnicodeDecodeError):
            pass

        started = time.monotonic()
        response = super().dispatch(request, *args, **kwargs)
        duration_ms = round((time.monotonic() - started) * 1000)
        response['X-Correlation-ID'] = correlation_id
        outcome = 'completed'
        try:
            response_payload = json.loads(response.content)
            if isinstance(response_payload, dict) and response_payload.get('errors'):
                outcome = 'error'
        except (AttributeError, TypeError, ValueError, UnicodeDecodeError):
            pass
        logger.info(
            'graphql_request operation=%s user=%s entity=- duration_ms=%s '
            'outcome=%s status=%s correlation_id=%s',
            operation_name or '-',
            request.user.pk,
            duration_ms,
            outcome,
            response.status_code,
            correlation_id,
        )
        return response
