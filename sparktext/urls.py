from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('messaging.urls', namespace='messaging')),
    path('', include('contacts.urls', namespace='contacts')),
    path('', include('compliance.urls', namespace='compliance')),
    path('', include('website.urls', namespace='website')),
    path("", include("core.urls")),
]
