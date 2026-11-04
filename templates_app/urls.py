from django.urls import path
from . import views

app_name = "templates"

urlpatterns = [
    path("templates/", views.templates_index, name="index"),
    path("templates/new/", views.template_create, name="create"),
    path("templates/<int:template_id>/edit/", views.template_edit, name="edit"),
    path("templates/<int:template_id>/delete/", views.template_delete, name="delete"),
    path("templates/api/list/", views.templates_api_list, name="api_list"),
]
