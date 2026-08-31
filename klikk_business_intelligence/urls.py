"""
URL configuration for klikk_business_intelligence project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include, re_path
from django.contrib.auth import views as auth_views
from rest_framework.authtoken.views import obtain_auth_token
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve as static_serve


def _excel_addin_asset(request, path, document_root=None):
    """Serve the add-in bundle with caching switched off.

    Excel's webview holds on to CSS and JS aggressively. The task pane HTML
    already sends no-store, but the assets it pulls did not, so a deploy could
    leave the pane running new markup against a stale stylesheet -- which looked
    exactly like broken CSS. Every client picks up a redeploy on next open.
    """
    response = static_serve(request, path, document_root=document_root)
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    response['Pragma'] = 'no-cache'
    return response

from apps.xero.xero_auth.views import XeroCallbackView

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # Authentication endpoints
    path('api/auth/', include('apps.user.urls')),  # JWT registration/login
    path('api/v2/auth/', include('apps.web_api_v2.auth_urls')),
    path('api/v2/entities/', include('apps.web_api_v2.entity_urls')),
    path('api/v2/graphql/', include('apps.web_api_v2.urls')),
    path('api-token-auth/', obtain_auth_token, name='api_token_auth'),  # Legacy token auth
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    
    # Xero endpoints (callback at /xero/callback/ for Xero redirect URI)
    path('xero/callback/', XeroCallbackView.as_view(), name='xero-callback'),
    path('xero/auth/', include('apps.xero.xero_auth.urls')),
    path('xero/core/', include('apps.xero.xero_core.urls')),
    path('xero/sync/', include('apps.xero.xero_sync.urls')),
    path('xero/data/', include('apps.xero.xero_data.urls')),
    path('xero/cube/', include('apps.xero.xero_cube.urls')),
    path('xero/metadata/', include('apps.xero.xero_metadata.urls')),
    path('xero/validation/', include('apps.xero.xero_validation.urls')),
    path('api/investec/', include('apps.investec.urls')),
    path('api/financial-investments/', include('apps.financial_investments.urls')),
    path('api/personal-expenses/', include('apps.personal_expenses.urls')),

    # Planning Analytics
    path('api/planning-analytics/', include('apps.planning_analytics.urls')),
    path('api/ai-agent/', include('apps.ai_agent.urls')),
    path('api/pricelist/', include('apps.pricelist.urls')),  # Equipment rate card + quote builder
    path('api/kb/', include('apps.kb.urls')),  # Books knowledge base (read-only)
    path('api/whatsapp/', include('apps.whatsapp_data.urls')),  # WhatsApp mirror (read-only, for MCP)

    # Audit -> Receipts review (raw SQL over whatsapp.klikk_slips + review tables); must precede audit/
    path('audit/receipts/', include('apps.receipts.urls')),

    # Year-end audit check registry (read-only; raw SQL over audit.checks)
    path('audit/', include('apps.audit.urls')),

    # Deployment webhook
    path('deployment/', include('apps.deployment.urls')),

    # Excel add-in static bundle (manifest + task pane). Public by design:
    # Office fetches these before any token exists, and they hold no secrets.
    re_path(
        r'^excel-addin/(?P<path>.*)$',
        _excel_addin_asset,
        {'document_root': settings.BASE_DIR / 'excel_addin'},
        name='excel_addin',
    ),
]

# Serve static and media files
# In production, use a web server (nginx, Apache) or WhiteNoise middleware to serve these files
# For staging, we'll serve them via Django for convenience (even when DEBUG=False)
if hasattr(settings, 'STATIC_ROOT') and settings.STATIC_ROOT:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
if hasattr(settings, 'MEDIA_URL') and hasattr(settings, 'MEDIA_ROOT') and settings.MEDIA_ROOT:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
