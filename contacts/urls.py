from django.urls import path

from . import views

app_name = "contacts"

urlpatterns = [
    # Contacts List & Core Operations
    path("contacts/", views.contacts_list, name="list"),
    path("contacts/export/", views.export_contacts_csv, name="export_contacts"),
    path("contacts/new/", views.add_contact, name="add"),
    path("contacts/bulk-action/", views.contacts_bulk_action, name="bulk_action"),
    path("contacts/<int:contact_id>/", views.contact_detail, name="detail"),
    path("contacts/<int:contact_id>/edit/", views.edit_contact, name="edit"),
    path("contacts/<int:contact_id>/delete/", views.delete_contact, name="delete"),
    path("contacts/<int:contact_id>/send/", views.send_contact_message, name="send_message"),
    path("contacts/<int:contact_id>/summary/", views.summarize_contact_conversation, name="summarize_conversation"),

    # CSV Import
    path("contacts/import/preview/", views.import_preview, name="import_preview"),
    path("contacts/import/process/", views.import_process, name="import_process"),

    # Contact Lists Management
    path("contacts/lists/", views.lists_index, name="lists"),
    path("contacts/lists/new/", views.create_list, name="create_list"),
    path("contacts/lists/<int:list_id>/", views.list_detail, name="list_detail"),
    path("contacts/lists/<int:list_id>/edit/", views.edit_list, name="edit_list"),
    path("contacts/lists/<int:list_id>/delete/", views.delete_list, name="delete_list"),
    path("contacts/lists/<int:list_id>/add-contacts/", views.add_contacts_to_list, name="add_contacts_to_list"),

    # Custom Contact Fields Management
    path("contacts/fields/", views.fields_index, name="fields"),
    path("contacts/fields/new/", views.create_field, name="create_field"),
    path("contacts/fields/<int:field_id>/edit/", views.edit_field, name="edit_field"),
    path("contacts/fields/<int:field_id>/delete/", views.delete_field, name="delete_field"),

    # Lead Ingestion REST / Webhook API & Remote URL Import
    path("contacts/api/leads/", views.lead_ingest_api, name="api_leads"),
    path("contacts/api/leads/<str:api_key>/", views.lead_ingest_api, name="api_leads_with_key"),
    path("contacts/api/fetch-remote/", views.fetch_remote_leads_view, name="fetch_remote_leads"),
    path("contacts/api/regenerate-key/", views.regenerate_api_key_view, name="regenerate_api_key"),
]
