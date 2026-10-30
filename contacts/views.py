import json
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Count
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST, require_GET, require_http_methods

from .models import Contact, ContactField, ContactList, ImportBatch
from .services import (
    save_contact,
    update_contact,
    preview_csv,
    suggest_column_mappings,
    import_contacts_csv,
)


@login_required
def contacts_list(request):
    """
    Main Contacts management page:
    - Lists organization's contacts with pagination
    - Filter by sms_status (opted_in, opted_out, unknown)
    - Search by phone, name, email
    - Provides ContactLists and ContactFields for modals/actions
    """
    org = request.user.organization
    if not org:
        return render(request, "contacts/no_organization.html")

    contacts_qs = Contact.objects.filter(organization=org).order_by("-created_at")

    # Search query
    q = request.GET.get("q", "").strip()
    if q:
        contacts_qs = contacts_qs.filter(
            Q(phone__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(email__icontains=q)
        )

    # Status filter
    status = request.GET.get("status", "").strip()
    if status in Contact.SMSStatus.values:
        contacts_qs = contacts_qs.filter(sms_status=status)

    # List filter
    list_id = request.GET.get("list", "").strip()
    selected_list = None
    if list_id:
        try:
            selected_list = ContactList.objects.get(id=list_id, organization=org)
            contacts_qs = contacts_qs.filter(lists=selected_list)
        except (ContactList.DoesNotExist, ValueError):
            pass

    # Status counts for filter pills
    status_counts = {
        "all": Contact.objects.filter(organization=org).count(),
        "opted_in": Contact.objects.filter(organization=org, sms_status=Contact.SMSStatus.OPTED_IN).count(),
        "opted_out": Contact.objects.filter(organization=org, sms_status=Contact.SMSStatus.OPTED_OUT).count(),
        "unknown": Contact.objects.filter(organization=org, sms_status=Contact.SMSStatus.UNKNOWN).count(),
    }

    paginator = Paginator(contacts_qs, 25)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    contact_lists = ContactList.objects.filter(organization=org).order_by("name")
    approved_fields = list(ContactField.objects.filter(organization=org).order_by("label"))

    # Discover any custom field keys present in page contacts that might not be in ContactField
    known_keys = {f.key for f in approved_fields}
    extra_keys = set()
    for c in page_obj:
        if isinstance(c.custom_fields, dict):
            for k in c.custom_fields.keys():
                if k not in known_keys:
                    extra_keys.add(k)

    class VirtualField:
        def __init__(self, key):
            self.key = key
            self.label = key.replace("_", " ").title()

    all_display_fields = list(approved_fields) + [VirtualField(k) for k in sorted(extra_keys)]

    # Attach custom field values mapping to each contact for direct column rendering
    for c in page_obj:
        cf = c.custom_fields if isinstance(c.custom_fields, dict) else {}
        c.custom_values_list = [
            {"key": f.key, "label": f.label, "value": cf.get(f.key, "")}
            for f in all_display_fields
        ]

    context = {
        "org": org,
        "page_obj": page_obj,
        "q": q,
        "status": status,
        "selected_list": selected_list,
        "status_counts": status_counts,
        "contact_lists": contact_lists,
        "contact_fields": all_display_fields,
    }
    return render(request, "contacts/contacts_list.html", context)


@login_required
@require_POST
def add_contact(request):
    """
    Handles manual single contact creation via AJAX modal or standard POST.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization associated with this account."}, status=400)

    phone = request.POST.get("phone", "").strip()
    first_name = request.POST.get("first_name", "").strip()
    last_name = request.POST.get("last_name", "").strip()
    email = request.POST.get("email", "").strip()
    timezone = request.POST.get("timezone", "").strip()
    contact_list_id = request.POST.get("contact_list_id", "").strip()

    # Process custom fields
    custom_fields = {}
    for key, value in request.POST.items():
        if key.startswith("custom_field_"):
            field_key = key.replace("custom_field_", "", 1)
            if value.strip():
                custom_fields[field_key] = value.strip()

    data = {
        "phone": phone,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "timezone": timezone,
        "custom_fields": custom_fields,
    }

    try:
        contact, created = save_contact(
            organization=org,
            data=data,
            source=Contact.Source.MANUAL,
        )

        # Attach to contact list if selected
        if contact_list_id:
            try:
                cl = ContactList.objects.get(id=contact_list_id, organization=org)
                cl.contacts.add(contact)
            except ContactList.DoesNotExist:
                pass

        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
            return JsonResponse({
                "status": "success",
                "created": created,
                "phone": contact.phone,
                "first_name": contact.first_name,
                "last_name": contact.last_name,
            })
        return redirect("contacts:list")

    except ValidationError as ve:
        err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
            return JsonResponse({"status": "error", "message": err_msg}, status=400)
        return render(request, "contacts/contacts_list.html", {"error": err_msg})


@login_required
@require_POST
def import_preview(request):
    """
    CSV Step 1: Takes uploaded CSV file, decodes it, returns column headers,
    sample rows, and auto-suggested mappings.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return JsonResponse({"status": "error", "message": "Please select a CSV file."}, status=400)

    try:
        raw_bytes = uploaded_file.read()
        preview = preview_csv(raw_bytes, sample_size=5)

        if not preview["headers"]:
            return JsonResponse({"status": "error", "message": "The CSV file appears to be empty."}, status=400)

        suggestions = suggest_column_mappings(org, preview["headers"])
        approved_fields = list(ContactField.objects.filter(organization=org).values("key", "label"))

        return JsonResponse({
            "status": "success",
            "headers": preview["headers"],
            "rows": preview["rows"],
            "duplicate_headers": preview["duplicate_headers"],
            "warning": preview["warning"],
            "suggestions": suggestions,
            "approved_fields": approved_fields,
        })
    except ValidationError as ve:
        err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        return JsonResponse({"status": "error", "message": err_msg}, status=400)
    except Exception as exc:
        return JsonResponse({"status": "error", "message": f"Could not read CSV file: {str(exc)}"}, status=400)


@login_required
@require_POST
def import_process(request):
    """
    CSV Step 3: Takes file and user's finalized column_mapping decisions,
    runs import_contacts_csv, and returns detailed results.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return JsonResponse({"status": "error", "message": "Missing file."}, status=400)

    raw_mapping = request.POST.get("mapping", "{}")
    try:
        mapping = json.loads(raw_mapping)
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid mapping format."}, status=400)

    contact_list_id = request.POST.get("contact_list_id", "").strip()
    target_list = None
    if contact_list_id:
        try:
            target_list = ContactList.objects.get(id=contact_list_id, organization=org)
        except ContactList.DoesNotExist:
            pass

    consent_attested = request.POST.get("consent_attested", "").lower() in ("true", "1", "yes", "on")
    attestation_text = request.POST.get("attestation_text", "").strip()

    try:
        report = import_contacts_csv(
            organization=org,
            file_or_content=uploaded_file.read(),
            column_mapping=mapping,
            contact_list=target_list,
            consent_attestation=consent_attested,
            attestation_text=attestation_text,
            filename=uploaded_file.name,
        )
        return JsonResponse({
            "status": "success",
            "report": report,
        })
    except ValidationError as ve:
        err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        return JsonResponse({"status": "error", "message": err_msg}, status=400)
    except Exception as exc:
        return JsonResponse({"status": "error", "message": f"Import failed: {str(exc)}"}, status=400)


# =========================================================================
# Contact Detail, Edit, and Delete Views
# =========================================================================

@login_required
def contact_detail(request, contact_id):
    """
    Detailed profile view for a single Contact:
    - View and edit all contact fields (name, phone, email, timezone, custom fields)
    - View lists the contact belongs to, with option to manage list assignments
    - Complete timeline of consent events from the append-only ledger (with exact proof text)
    """
    org = request.user.organization
    if not org:
        return render(request, "contacts/no_organization.html")

    contact = get_object_or_404(Contact, id=contact_id, organization=org)
    consent_events = contact.consent_events.all().order_by("-created_at")
    all_lists = ContactList.objects.filter(organization=org).order_by("name")
    contact_list_ids = set(contact.lists.values_list("id", flat=True))
    all_fields = ContactField.objects.filter(organization=org).order_by("label")

    context = {
        "org": org,
        "contact": contact,
        "consent_events": consent_events,
        "all_lists": all_lists,
        "contact_list_ids": contact_list_ids,
        "all_fields": all_fields,
    }
    return render(request, "contacts/contact_detail.html", context)


@login_required
@require_POST
def edit_contact(request, contact_id):
    """
    Updates an existing contact's details and custom fields without duplicating.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    contact = get_object_or_404(Contact, id=contact_id, organization=org)

    phone = request.POST.get("phone", contact.phone).strip()
    first_name = request.POST.get("first_name", "").strip()
    last_name = request.POST.get("last_name", "").strip()
    email = request.POST.get("email", "").strip()
    timezone = request.POST.get("timezone", "").strip()

    # Process custom fields
    custom_fields = {}
    for key, value in request.POST.items():
        if key.startswith("custom_field_"):
            field_key = key.replace("custom_field_", "", 1)
            if value.strip():
                custom_fields[field_key] = value.strip()

    data = {
        "phone": phone,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "timezone": timezone,
        "custom_fields": custom_fields,
    }

    try:
        updated = update_contact(contact=contact, data=data)

        # Update lists if provided in POST
        list_ids = request.POST.getlist("lists")
        if list_ids or "lists_submitted" in request.POST:
            valid_lists = ContactList.objects.filter(id__in=list_ids, organization=org)
            updated.lists.set(valid_lists)

        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
            return JsonResponse({
                "status": "success",
                "phone": updated.phone,
                "first_name": updated.first_name,
                "last_name": updated.last_name,
            })
        return redirect("contacts:detail", contact_id=updated.id)

    except ValidationError as ve:
        err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
            return JsonResponse({"status": "error", "message": err_msg}, status=400)
        return redirect("contacts:detail", contact_id=contact.id)
    except Exception as exc:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
            return JsonResponse({"status": "error", "message": str(exc)}, status=400)
        return redirect("contacts:detail", contact_id=contact.id)


@login_required
@require_POST
def delete_contact(request, contact_id):
    """
    Permanently deletes a contact.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    contact = get_object_or_404(Contact, id=contact_id, organization=org)
    phone = contact.phone
    contact.delete()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
        return JsonResponse({"status": "success", "message": f"Contact {phone} deleted."})
    return redirect("contacts:list")


@login_required
@require_POST
def contacts_bulk_action(request):
    """
    Handles bulk operations on selected contacts:
    - add_to_list: assign selected contacts to a ContactList
    - remove_from_list: unassign selected contacts from a ContactList
    - delete: bulk delete selected contacts
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    action = request.POST.get("action", "").strip()
    list_id = request.POST.get("list_id", "").strip()
    contact_ids_raw = request.POST.getlist("contact_ids")
    if not contact_ids_raw:
        # Check if single JSON array passed
        try:
            contact_ids_raw = json.loads(request.POST.get("contact_ids_json", "[]"))
        except json.JSONDecodeError:
            contact_ids_raw = []

    if not contact_ids_raw:
        return JsonResponse({"status": "error", "message": "No contacts selected."}, status=400)

    contacts_qs = Contact.objects.filter(id__in=contact_ids_raw, organization=org)
    count = contacts_qs.count()

    if action == "add_to_list":
        if not list_id:
            return JsonResponse({"status": "error", "message": "Please choose a target list."}, status=400)
        target_list = get_object_or_404(ContactList, id=list_id, organization=org)
        target_list.contacts.add(*contacts_qs)
        msg = f"Added {count} contact(s) to '{target_list.name}'."

    elif action == "remove_from_list":
        if not list_id:
            return JsonResponse({"status": "error", "message": "Please choose a target list."}, status=400)
        target_list = get_object_or_404(ContactList, id=list_id, organization=org)
        target_list.contacts.remove(*contacts_qs)
        msg = f"Removed {count} contact(s) from '{target_list.name}'."

    elif action == "delete":
        contacts_qs.delete()
        msg = f"Deleted {count} contact(s)."

    else:
        return JsonResponse({"status": "error", "message": f"Unsupported action '{action}'."}, status=400)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
        return JsonResponse({"status": "success", "message": msg, "count": count})
    return redirect("contacts:list")


# =========================================================================
# Contact Lists Management Views
# =========================================================================

@login_required
def lists_index(request):
    """
    Overview page of all contact lists/segments for the organization.
    """
    org = request.user.organization
    if not org:
        return render(request, "contacts/no_organization.html")

    lists_qs = (
        ContactList.objects.filter(organization=org)
        .annotate(contact_count=Count("contacts"))
        .order_by("name")
    )

    context = {
        "org": org,
        "lists": lists_qs,
        "total_lists": lists_qs.count(),
    }
    return render(request, "contacts/lists_index.html", context)


@login_required
@require_POST
def create_list(request):
    """
    Create a new ContactList for the organization.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"status": "error", "message": "List name cannot be blank."}, status=400)

    if ContactList.objects.filter(organization=org, name__iexact=name).exists():
        return JsonResponse({"status": "error", "message": f"A list named '{name}' already exists."}, status=400)

    cl = ContactList.objects.create(organization=org, name=name)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
        return JsonResponse({"status": "success", "id": cl.id, "name": cl.name})
    return redirect("contacts:lists")


@login_required
@require_POST
def edit_list(request, list_id):
    """
    Rename an existing ContactList.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    cl = get_object_or_404(ContactList, id=list_id, organization=org)
    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"status": "error", "message": "List name cannot be blank."}, status=400)

    if ContactList.objects.filter(organization=org, name__iexact=name).exclude(id=cl.id).exists():
        return JsonResponse({"status": "error", "message": f"A list named '{name}' already exists."}, status=400)

    cl.name = name
    cl.save()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
        return JsonResponse({"status": "success", "id": cl.id, "name": cl.name})
    return redirect("contacts:lists")


@login_required
@require_POST
def delete_list(request, list_id):
    """
    Delete a ContactList. Contacts themselves remain in the database.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    cl = get_object_or_404(ContactList, id=list_id, organization=org)
    name = cl.name
    cl.delete()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
        return JsonResponse({"status": "success", "message": f"List '{name}' deleted."})
    return redirect("contacts:lists")


# =========================================================================
# Custom Contact Fields Management Views
# =========================================================================

@login_required
def fields_index(request):
    """
    Overview page of approved extra fields for the organization.
    """
    org = request.user.organization
    if not org:
        return render(request, "contacts/no_organization.html")

    fields_qs = ContactField.objects.filter(organization=org).order_by("label")

    context = {
        "org": org,
        "fields": fields_qs,
        "total_fields": fields_qs.count(),
    }
    return render(request, "contacts/fields_index.html", context)


@login_required
@require_POST
def create_field(request):
    """
    Create a new approved custom field for the organization.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    key = request.POST.get("key", "").strip().lower()
    label = request.POST.get("label", "").strip()

    if not label:
        return JsonResponse({"status": "error", "message": "Field label cannot be blank."}, status=400)

    if not key:
        import re
        key = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_")
        if not key or not key[0].isalpha():
            key = f"field_{key}"

    field = ContactField(organization=org, key=key, label=label)
    try:
        field.full_clean()
        field.save()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
            return JsonResponse({"status": "success", "id": field.id, "key": field.key, "label": field.label})
        return redirect("contacts:fields")
    except ValidationError as ve:
        err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        return JsonResponse({"status": "error", "message": err_msg}, status=400)


@login_required
@require_POST
def edit_field(request, field_id):
    """
    Update the display label of an approved custom field (key is read-only).
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    field = get_object_or_404(ContactField, id=field_id, organization=org)
    label = request.POST.get("label", "").strip()
    if not label:
        return JsonResponse({"status": "error", "message": "Field label cannot be blank."}, status=400)

    field.label = label
    field.save(update_fields=["label"])

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
        return JsonResponse({"status": "success", "id": field.id, "label": field.label})
    return redirect("contacts:fields")


@login_required
@require_POST
def delete_field(request, field_id):
    """
    Delete an approved custom field from the organization.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    field = get_object_or_404(ContactField, id=field_id, organization=org)
    key = field.key
    field.delete()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
        return JsonResponse({"status": "success", "message": f"Field '{key}' deleted."})
    return redirect("contacts:fields")

