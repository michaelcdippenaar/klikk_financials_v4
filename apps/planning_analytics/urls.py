from django.urls import path
from apps.planning_analytics.views import (
    PipelineRunView,
    TM1ExecuteView,
    TM1TestConnectionView,
    TM1ConfigView,
    TM1ProcessListView,
    UserTM1CredentialsView,
    TrackingMappingView,
    TrackingMappingAddView,
)

urlpatterns = [
    path('pipeline/run/', PipelineRunView.as_view(), name='planning-analytics-pipeline-run'),
    path('tm1/execute/', TM1ExecuteView.as_view(), name='planning-analytics-tm1-execute'),
    path('tm1/test-connection/', TM1TestConnectionView.as_view(), name='planning-analytics-tm1-test'),
    path('tm1/config/', TM1ConfigView.as_view(), name='planning-analytics-tm1-config'),
    path('tm1/processes/', TM1ProcessListView.as_view(), name='planning-analytics-tm1-processes'),
    path('tm1/credentials/', UserTM1CredentialsView.as_view(), name='planning-analytics-tm1-credentials'),
    path('tm1/tracking-mapping/', TrackingMappingView.as_view(), name='planning-analytics-tracking-mapping'),
    path('tm1/tracking-mapping/add/', TrackingMappingAddView.as_view(), name='planning-analytics-tracking-mapping-add'),
]
