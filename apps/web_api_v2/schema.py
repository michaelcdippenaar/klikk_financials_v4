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

from .queries.ingest_overview import build_ingest_overview
from .queries.viewer_context import build_viewer_context
from .types.ingest import IngestOverview, IngestOverviewInput
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


def _resolve_safely(info, operation, resolver):
    try:
        return resolver()
    except GraphQLError:
        raise
    except DatabaseError:
        request = info.context.request
        correlation_id = getattr(request, 'graphql_correlation_id', '-')
        logger.error(
            'graphql_data_unavailable operation=%s user=%s correlation_id=%s',
            operation,
            request.user.pk,
            correlation_id,
        )
        raise GraphQLError(
            'Requested data is temporarily unavailable.',
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
            'graphql_data_failed operation=%s user=%s correlation_id=%s',
            operation,
            request.user.pk,
            correlation_id,
        )
        raise GraphQLError(
            'Unable to load requested data.',
            extensions={
                'code': 'INTERNAL_ERROR',
                'correlationId': correlation_id,
            },
        ) from None


@strawberry.type
class Query:
    @strawberry.field
    def viewer_context(self, info: strawberry.Info) -> ViewerContext:
        return _resolve_safely(
            info,
            'viewerContext',
            lambda: build_viewer_context(info),
        )

    @strawberry.field
    def ingest_overview(
        self,
        info: strawberry.Info,
        input: IngestOverviewInput,
    ) -> IngestOverview:
        return _resolve_safely(
            info,
            'ingestOverview',
            lambda: build_ingest_overview(info, input),
        )


schema = SafeSchema(
    query=Query,
    extensions=[
        DisableIntrospection,
        lambda: QueryDepthLimiter(max_depth=settings.WEB_API_V2_MAX_QUERY_DEPTH),
        lambda: MaxTokensLimiter(max_token_count=settings.WEB_API_V2_MAX_QUERY_TOKENS),
        lambda: AddValidationRules([MaxFieldSelectionsRule]),
    ],
)
