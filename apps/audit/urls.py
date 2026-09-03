"""URL map for the audit app: the year-end check registry and the findings register.

Route ordering rule that matters here: every FIXED-segment findings route
(``summary/``, ``export/``, ``bulk/``, ``graph/``, ``links/<pk>/``,
``attachments/<pk>/``) is declared ABOVE ``findings/<int:pk>/``. Django resolves in
order, and while the ``int`` converter would not in fact swallow a word like
``graph``, relying on that is a trap for the next person who adds a
``<str:...>`` route. Keep fixed segments first.

The signed attachment viewer lives at ``findings/attachments/<pk>/file/`` —
PLURAL. It was singular in the first cut; ``findings_services.attachment_url()``
now emits the plural form, and the two must agree or every ``view_url`` the API
hands out is a dead link (that mismatch was caught by the adversarial suite, not
by review).
"""
from django.urls import path

from . import (
    comment_feed_views, cube_comment_views, findings_cube_views, findings_graph_views,
    findings_links_views, findings_views, views,
)
from .findings_file_view import finding_attachment_file_view
from .slip_view import slip_file_view

app_name = 'audit'

urlpatterns = [
    # --- year-end check registry ---
    path('checks/', views.checks_view, name='checks'),
    path('checks/<str:code>/', views.check_detail_view, name='check_detail'),
    path('checks/<str:code>/run/', views.run_check_view, name='run_check'),
    path('run/', views.run_audit_view, name='run_audit'),
    path('history/<str:code>/', views.history_view, name='history'),
    path('runs/', views.runs_view, name='runs'),
    path('slip/<str:sha256>/', slip_file_view, name='slip_file'),

    # --- live comment feed (polled by the console; see comment_feed_views) ---
    path('comments/feed/', comment_feed_views.comment_feed_view, name='comment_feed'),

    # --- cube-comment register + reply threads (see cube_comment_views) ---
    # Mounted HERE, under /audit/, because that prefix is what the auditor gate
    # reads. The register itself lives in apps.xero.xero_data; only these two
    # routes are exposed to auditors, and /xero/data/... stays shut.
    path('cube-comments/', cube_comment_views.cube_comments_view, name='cube_comments'),
    path('cube-comments/<int:comment_id>/replies/',
         cube_comment_views.cube_comment_replies_view, name='cube_comment_replies'),

    # --- findings register: fixed-segment routes (MUST precede findings/<int:pk>/) ---
    path('findings/summary/', findings_views.findings_summary_view, name='findings_summary'),
    path('findings/export/', findings_views.findings_export_view, name='findings_export'),
    path('findings/bulk/', findings_views.findings_bulk_view, name='findings_bulk'),
    path('findings/graph/', findings_graph_views.findings_graph_view, name='findings_graph'),
    path('findings/attachments/<int:pk>/file/', finding_attachment_file_view,
         name='finding_attachment_file'),
    path('findings/attachments/<int:pk>/', findings_views.finding_attachment_delete_view,
         name='finding_attachment_delete'),
    path('findings/links/<int:pk>/', findings_links_views.finding_link_delete_view,
         name='finding_link_delete'),

    # --- findings register: collection + per-finding routes ---
    path('findings/', findings_views.findings_view, name='findings'),
    path('findings/<int:pk>/', findings_views.finding_detail_view, name='finding_detail'),
    path('findings/<int:pk>/comments/', findings_views.finding_comments_view, name='finding_comments'),
    path('findings/<int:pk>/attachments/', findings_views.finding_attachments_view,
         name='finding_attachments'),
    path('findings/<int:pk>/links/', findings_links_views.finding_links_view, name='finding_links'),
    path('findings/<int:pk>/cube-view/', findings_cube_views.finding_cube_view_view,
         name='finding_cube_view'),
    path('findings/<int:pk>/cube-view/data/', findings_cube_views.finding_cube_data_view,
         name='finding_cube_data'),
    path('findings/<int:pk>/cube-view/suggest/', findings_cube_views.finding_cube_suggest_view,
         name='finding_cube_suggest'),
]
