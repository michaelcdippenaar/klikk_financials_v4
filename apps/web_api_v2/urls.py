from django.urls import path

from .schema import schema
from .views import AuthenticatedGraphQLView


app_name = 'web_api_v2'

urlpatterns = [
    path('', AuthenticatedGraphQLView.as_view(schema=schema, graphql_ide=None), name='graphql'),
]
