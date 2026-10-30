from django.urls import path
from . import views

app_name = "contacts"

urlpatterns = [
    # Contacts List & Core Operations
    path("contacts/", views.contacts_list, name="list"),
    path("contacts/new/", views.add_contact, name="add"),
    path("contacts/bulk-action/", views.contacts_bulk_action, name="bulk_action"),
    path("contacts/<int:contact_id>/", views.contact_detail, name="detail"),
    path("contacts/<int:contact_id>/edit/", views.edit_contact, name="edit"),
    path("contacts/<int:contact_id>/delete/", views.delete_contact, name="delete"),

    # CSV Import
    path("contacts/import/preview/", views.import_preview, name="import_preview"),
    path("contacts/import/process/", views.import_process, name="import_process"),

    # Contact Lists Management
    path("contacts/lists/", views.lists_index, name="lists"),
    path("contacts/lists/new/", views.create_list, name="create_list"),
    path("contacts/lists/<int:list_id>/edit/", views.edit_list, name="edit_list"),
    path("contacts/lists/<int:list_id>/delete/", views.delete_list, name="delete_list"),

    # Custom Contact Fields Management
    path("contacts/fields/", views.fields_index, name="fields"),
    path("contacts/fields/new/", views.create_field, name="create_field"),
    path("contacts/fields/<int:field_id>/edit/", views.edit_field, name="edit_field"),
    path("contacts/fields/<int:field_id>/delete/", views.delete_field, name="delete_field"),
]
