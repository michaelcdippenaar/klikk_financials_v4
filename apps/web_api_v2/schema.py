import logging

import strawberry
from django.conf import settings
from django.db import DatabaseError
from graphql import GraphQLError
from strawberry.extensions import (
    AddValidationRules,
    DisableIntrospection,
    MaxTokensLimiter,
    QueryDepthLimiter,
)

from .queries.viewer_context import build_viewer_context
from .types.viewer import ViewerContext
from .validation import MaxFieldSelectionsRule


logger = logging.getLogger(__name__)


class SafeSchema(strawberry.Schema):
    def process_errors(self, errors, execution_context=None):
        """Log only safe metadata; Strawberry's default includes query source."""
        context = getattr(execution_context, 'context', None)
        request = getattr(context, 'request', None)
        logger.warning(
            'graphql_execution_errors user=%s error_count=%s correlation_id=%s',
            getattr(getattr(request, 'user', None), 'pk', '-'),
            len(errors),
            getattr(request, 'graphql_correlation_id', '-'),
        )


@strawberry.type
class Query:
    @strawberry.field
    def viewer_context(self, info: strawberry.Info) -> ViewerContext:
        try:
            return build_viewer_context(info)
        except GraphQLError:
            raise
        except DatabaseError:
            request = info.context.request
            correlation_id = getattr(request, 'graphql_correlation_id', '-')
            logger.error(
                'viewer_context_unavailable user=%s correlation_id=%s',
                request.user.pk,
                correlation_id,
            )
            raise GraphQLError(
                'Viewer context is temporarily unavailable.',
                extensions={
                    'code': 'TEMPORARILY_UNAVAILABLE',
                    'correlationId': correlation_id,
                    'retryable': True,
                },
            ) from None
        except Exception:
            request = info.context.request
            correlation_id = getattr(request, 'graphql_correlation_id', '-')
            logger.error(
                'viewer_context_failed user=%s correlation_id=%s',
                request.user.pk,
                correlation_id,
            )
            raise GraphQLError(
                'Unable to load viewer context.',
                extensions={
                    'code': 'INTERNAL_ERROR',
                    'correlationId': correlation_id,
                },
            ) from None


schema = SafeSchema(
    query=Query,
    extensions=[
        DisableIntrospection,
        lambda: QueryDepthLimiter(max_depth=settings.WEB_API_V2_MAX_QUERY_DEPTH),
        lambda: MaxTokensLimiter(max_token_count=settings.WEB_API_V2_MAX_QUERY_TOKENS),
        lambda: AddValidationRules([MaxFieldSelectionsRule]),
    ],
)
