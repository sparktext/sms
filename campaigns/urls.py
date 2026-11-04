from django.urls import path
from . import views

app_name = "campaigns"

urlpatterns = [
    path("campaigns/", views.campaign_list, name="list"),
    path("campaigns/new/", views.campaign_create, name="create"),
    path("campaigns/<int:campaign_id>/", views.campaign_detail, name="detail"),
    path("campaigns/<int:campaign_id>/action/", views.campaign_action, name="action"),
    path("campaigns/api/audience-preview/", views.audience_preview_ajax, name="audience_preview"),
]

