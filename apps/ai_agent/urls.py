from django.urls import path

from .views import (
    AgentHealthView,
    AgentStatusView,
    GlossaryRefreshView,
    AgentProjectDetailView,
    AgentProjectImportTM1DocsView,
    AgentProjectListCreateView,
    KnowledgeCorpusListCreateView,
    KnowledgeCorpusSearchView,
    KnowledgeCorpusVectorizeView,
    AgentSessionListCreateView,
    AgentSessionImportCursorChatView,
    AgentSessionMemoryView,
    AgentSessionMessageCreateView,
    AgentSessionExportToSystemDocView,
    AgentSessionRunView,
    AgentSessionRunWithToolsView,
    AgentSessionToolExecutionsView,
    SystemDocumentDetailView,
    SystemDocumentGenerateView,
    SystemDocumentListCreateView,
    TM1ConfigView,
    TM1ProxyExecuteView,
    TM1ProxyTestConnectionView,
    TM1VersionView,
    # MCP Skills Engine views
    SkillRegistryListView,
    SkillRegistryDetailView,
    CredentialListView,
    CredentialDetailView,
    MCPAgentChatView,
    AgentRunningStatusView,
    WebSocketBroadcastView,
    # Monitoring
    AgentMonitorPerformanceView,
    AgentMonitorSessionsView,
    AgentMonitorHealthView,
    AgentMonitorErrorsView,
    AgentMonitorSlowToolsView,
    AgentMonitorLiveView,
)

app_name = 'ai_agent'

urlpatterns = [
    path('health/', AgentHealthView.as_view(), name='health'),
    path('status/', AgentStatusView.as_view(), name='status'),
    path('glossary/refresh/', GlossaryRefreshView.as_view(), name='glossary-refresh'),
    path('projects/', AgentProjectListCreateView.as_view(), name='projects'),
    path('projects/<int:project_id>/', AgentProjectDetailView.as_view(), name='project-detail'),
    path('projects/<int:project_id>/import-tm1-docs/', AgentProjectImportTM1DocsView.as_view(), name='project-import-tm1-docs'),
    path('corpora/', KnowledgeCorpusListCreateView.as_view(), name='corpora'),
    path('corpora/<int:corpus_id>/vectorize/', KnowledgeCorpusVectorizeView.as_view(), name='corpus-vectorize'),
    path('corpora/<int:corpus_id>/search/', KnowledgeCorpusSearchView.as_view(), name='corpus-search'),
    path('sessions/', AgentSessionListCreateView.as_view(), name='sessions'),
    path('sessions/<int:session_id>/messages/', AgentSessionMessageCreateView.as_view(), name='session-messages'),
    path('sessions/<int:session_id>/memory/', AgentSessionMemoryView.as_view(), name='session-memory'),
    path('sessions/<int:session_id>/import-cursor-chat/', AgentSessionImportCursorChatView.as_view(), name='session-import-cursor-chat'),
    path('sessions/<int:session_id>/export-to-system-doc/', AgentSessionExportToSystemDocView.as_view(), name='session-export-to-system-doc'),
    path('sessions/<int:session_id>/run/', AgentSessionRunView.as_view(), name='session-run'),
    path('sessions/<int:session_id>/run-with-tools/', AgentSessionRunWithToolsView.as_view(), name='session-run-with-tools'),
    path('sessions/<int:session_id>/executions/', AgentSessionToolExecutionsView.as_view(), name='session-executions'),
    path('system-docs/', SystemDocumentListCreateView.as_view(), name='system-docs'),
    path('system-docs/generate/', SystemDocumentGenerateView.as_view(), name='system-doc-generate'),
    path('system-docs/<int:doc_id>/', SystemDocumentDetailView.as_view(), name='system-doc-detail'),
    path('tm1/config/', TM1ConfigView.as_view(), name='tm1-config'),
    path('tm1/proxy/', TM1ProxyExecuteView.as_view(), name='tm1-proxy'),
    path('tm1/test-connection/', TM1ProxyTestConnectionView.as_view(), name='tm1-test-connection'),
    path('tm1/version/', TM1VersionView.as_view(), name='tm1-version'),

    # MCP Skills Engine
    path('skills/registry/', SkillRegistryListView.as_view(), name='skill-registry-list'),
    path('skills/registry/<str:module_name>/', SkillRegistryDetailView.as_view(), name='skill-registry-detail'),
    path('credentials/', CredentialListView.as_view(), name='credential-list'),
    path('credentials/<str:key>/', CredentialDetailView.as_view(), name='credential-detail'),
    path('mcp/chat/', MCPAgentChatView.as_view(), name='mcp-chat'),
    path('agent-status/', AgentRunningStatusView.as_view(), name='agent-running-status'),

    # WebSocket bridge (called by FastAPI to broadcast to observers)
    path('ws/broadcast/', WebSocketBroadcastView.as_view(), name='ws-broadcast'),

    # Agent Monitoring Dashboard
    path('monitor/performance/', AgentMonitorPerformanceView.as_view(), name='monitor-performance'),
    path('monitor/sessions/', AgentMonitorSessionsView.as_view(), name='monitor-sessions'),
    path('monitor/health/', AgentMonitorHealthView.as_view(), name='monitor-health'),
    path('monitor/errors/', AgentMonitorErrorsView.as_view(), name='monitor-errors'),
    path('monitor/slow-tools/', AgentMonitorSlowToolsView.as_view(), name='monitor-slow-tools'),
    path('monitor/live/', AgentMonitorLiveView.as_view(), name='monitor-live'),
]

