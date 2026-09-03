"""
Activity-trail URLs, mounted at /api/activity/ — OUTSIDE /audit/ on purpose.

Auditors are the accounts this trail records, and the auditor gate 403s every
non-/audit/ path for them, so the mount point is the access control.
"""
from django.urls import path

from . import views

app_name = 'activity'

urlpatterns = [
    # Fixed segments before the collection, per the house rule in apps/audit/urls.py.
    path('actors/', views.activity_actors_view, name='actors'),
    path('actions/', views.activity_actions_view, name='actions'),
    path('export/', views.activity_export_view, name='export'),
    path('', views.activity_list_view, name='list'),
]
