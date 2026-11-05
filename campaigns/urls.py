from django.urls import path
from . import views

app_name = "campaigns"

urlpatterns = [
    path("campaigns/", views.campaign_list, name="list"),
    path("campaigns/new/", views.campaign_create, name="create"),
    path("campaigns/<int:campaign_id>/", views.campaign_detail, name="detail"),
    path("campaigns/<int:campaign_id>/action/", views.campaign_action, name="action"),
    path("campaigns/api/audience-preview/", views.audience_preview_ajax, name="audience_preview"),

    # Templates (inside Campaigns)
    path("campaigns/templates/", views.templates_index, name="templates"),
    path("campaigns/templates/new/", views.template_create, name="template_create"),
    path("campaigns/templates/<int:template_id>/edit/", views.template_edit, name="template_edit"),
    path("campaigns/templates/<int:template_id>/delete/", views.template_delete, name="template_delete"),
    path("campaigns/templates/api/list/", views.templates_api_list, name="templates_api_list"),
    path("campaigns/templates/reset-defaults/", views.templates_reset_defaults, name="templates_reset_defaults"),
]

