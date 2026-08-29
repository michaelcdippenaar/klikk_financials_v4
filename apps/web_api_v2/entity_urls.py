from django.urls import path

from .ingest_views import ProcessRunDetailView, ProcessRunListCreateView, ProcessStatusView
from .share_mapping_views import ShareMappingView


app_name = 'web_api_v2_entities'

urlpatterns = [
    path(
        '<str:entity_id>/ingest/process-runs/',
        ProcessRunListCreateView.as_view(),
        name='ingest-process-runs',
    ),
    path(
        '<str:entity_id>/ingest/process-runs/<uuid:run_id>/',
        ProcessRunDetailView.as_view(),
        name='ingest-process-run-detail',
    ),
    path(
        '<str:entity_id>/investec/share-mappings/',
        ShareMappingView.as_view(),
        name='investec-share-mappings',
    ),
    path(
        '<str:entity_id>/ingest/process-status/',
        ProcessStatusView.as_view(),
        name='ingest-process-status',
    ),
]
