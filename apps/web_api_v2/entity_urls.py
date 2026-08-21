from django.urls import path

from .ingest_views import ProcessRunDetailView, ProcessRunListCreateView, ProcessStatusView


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
        '<str:entity_id>/ingest/process-status/',
        ProcessStatusView.as_view(),
        name='ingest-process-status',
    ),
]
