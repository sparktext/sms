from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.views.static import serve
from django.views.generic import RedirectView
from contacts import views as contacts_views
from campaigns import views as campaigns_views

urlpatterns = [
    path("favicon.ico", RedirectView.as_view(url="/static/img/favicon.ico", permanent=True)),
    path("admin/", admin.site.urls),

    # --- django-allauth (provides /accounts/login/, /accounts/signup/, etc.) ---
    path("accounts/", include("allauth.urls")),

    # --- Custom SparkText auth views ---
    # Mounted at /accounts/ — shares the same prefix as allauth, different URL names
    path("accounts/", include(("accounts.urls", "accounts"), namespace="accounts")),

    # --- Anymail event webhooks ---
    path("anymail/", include("anymail.urls")),

    # --- Dashboard shell /app/<uuid:public_id>/ ---
    # Uses the accounts.urls patterns; the <uuid:public_id>/ pattern matches here
    path("app/", include(("accounts.urls", "accounts_shell"), namespace="accounts_shell")),

    # --- Top-level API endpoints ---
    path("api/v1/leads/", contacts_views.lead_ingest_api, name="api_v1_leads"),
    path("api/v1/leads/<str:api_key>/", contacts_views.lead_ingest_api, name="api_v1_leads_with_key"),
    path("templates/", campaigns_views.templates_index, name="top_level_templates"),

    # --- App-namespaced feature URLs ---
    path("", include("messaging.urls", namespace="messaging")),
    path("", include("contacts.urls", namespace="contacts")),
    path("", include("compliance.urls", namespace="compliance")),
    path("", include("campaigns.urls", namespace="campaigns")),
    path("", include("website.urls", namespace="website")),
    path("", include("core.urls")),
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]
