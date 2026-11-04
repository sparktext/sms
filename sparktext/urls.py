from django.contrib import admin
from django.urls import path, include
from contacts import views as contacts_views

urlpatterns = [
    path('admin/', admin.site.urls),
    # Top-level API endpoints
    path('api/v1/leads/', contacts_views.lead_ingest_api, name='api_v1_leads'),
    path('api/v1/leads/<str:api_key>/', contacts_views.lead_ingest_api, name='api_v1_leads_with_key'),
    path('', include('messaging.urls', namespace='messaging')),
    path('', include('contacts.urls', namespace='contacts')),
    path('', include('compliance.urls', namespace='compliance')),
    path('', include('campaigns.urls', namespace='campaigns')),
    path('', include('templates_app.urls', namespace='templates')),
    path('', include('website.urls', namespace='website')),
    path("", include("core.urls")),
]
