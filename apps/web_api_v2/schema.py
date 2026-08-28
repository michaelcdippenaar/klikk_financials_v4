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
from .queries.overview_ingest_sources import build_overview_ingest_sources
from .queries.source_connections import build_source_connections
from .queries.xero_pipeline import (
    build_xero_pipeline_run_detail,
    build_xero_pipeline_run_history,
    build_xero_pipeline_summary,
)
from .queries.xero_reconciliation import build_xero_reconciliation
from .queries.viewer_context import build_viewer_context
from .queries.xero_connection_status import build_xero_connection_status
from .types.financial_context import FinancialContextInput
from .types.ingest import IngestOverview, IngestOverviewInput
from .types.overview_ingest import OverviewIngestSources
from .types.source_connections import SourceConnections
from .types.xero_pipeline import (
    XeroPipelineRunDetail,
    XeroPipelineRunHistory,
    XeroPipelineStageKey,
    XeroPipelineSummary,
)
from .types.xero_reconciliation import XeroReconciliation
from .types.viewer import ViewerContext
from .types.xero_connection_status import XeroConnectionStatus
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
    except Exception as exc:
        request = info.context.request
        correlation_id = getattr(request, 'graphql_correlation_id', '-')
        logger.error(
            'graphql_data_failed operation=%s user=%s exception_type=%s correlation_id=%s',
            operation,
            request.user.pk,
            type(exc).__name__,
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

    @strawberry.field
    def overview_ingest_sources(
        self,
        info: strawberry.Info,
        context: FinancialContextInput,
    ) -> OverviewIngestSources:
        return _resolve_safely(
            info,
            'overviewIngestSources',
            lambda: build_overview_ingest_sources(info, context),
        )

    @strawberry.field
    def xero_reconciliation(
        self,
        info: strawberry.Info,
        context: FinancialContextInput,
    ) -> XeroReconciliation:
        return _resolve_safely(
            info,
            'xeroReconciliation',
            lambda: build_xero_reconciliation(info, context),
        )

    @strawberry.field
    def source_connections(
        self,
        info: strawberry.Info,
        context: FinancialContextInput,
    ) -> SourceConnections:
        return _resolve_safely(
            info,
            'sourceConnections',
            lambda: build_source_connections(info, context),
        )

    @strawberry.field
    def xero_connection_status(
        self,
        info: strawberry.Info,
        context: FinancialContextInput,
    ) -> XeroConnectionStatus:
        return _resolve_safely(
            info,
            'xeroConnectionStatus',
            lambda: build_xero_connection_status(info, context),
        )

    @strawberry.field
    def xero_pipeline_summary(
        self,
        info: strawberry.Info,
        context: FinancialContextInput,
    ) -> XeroPipelineSummary:
        return _resolve_safely(
            info,
            'xeroPipelineSummary',
            lambda: build_xero_pipeline_summary(info, context),
        )

    @strawberry.field
    def xero_pipeline_run_history(
        self,
        info: strawberry.Info,
        context: FinancialContextInput,
        stage: XeroPipelineStageKey,
        limit: int = 20,
    ) -> XeroPipelineRunHistory:
        return _resolve_safely(
            info,
            'xeroPipelineRunHistory',
            lambda: build_xero_pipeline_run_history(info, context, stage, limit),
        )

    @strawberry.field
    def xero_pipeline_run_detail(
        self,
        info: strawberry.Info,
        context: FinancialContextInput,
        run_id: strawberry.ID,
    ) -> XeroPipelineRunDetail:
        return _resolve_safely(
            info,
            'xeroPipelineRunDetail',
            lambda: build_xero_pipeline_run_detail(info, context, run_id),
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
