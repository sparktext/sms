import json

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

from .models import Contact, ContactField, ContactList, ImportBatch
from .services import save_contact, update_contact, validate_custom_fields


class ContactAdminForm(forms.ModelForm):
    custom_fields_json = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4, "cols": 40}),
        required=False,
        label=_("Custom fields (JSON)"),
        help_text=_("Key-value dictionary matching approved ContactFields."),
    )

    class Meta:
        model = Contact
        fields = [
            "organization",
            "phone",
            "first_name",
            "last_name",
            "email",
            "source",
            "custom_fields_json",
            "external_ids",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["organization"].disabled = True
            self.fields["custom_fields_json"].initial = json.dumps(
                self.instance.custom_fields, indent=2
            )
        else:
            self.fields["custom_fields_json"].initial = "{}"

    def clean_custom_fields_json(self):
        data = self.cleaned_data.get("custom_fields_json")
        if not data or not data.strip():
            return {}
        try:
            parsed = json.loads(data)
            if not isinstance(parsed, dict):
                raise ValidationError(_("Must be a valid JSON object/dict."))
            return parsed
        except json.JSONDecodeError as exc:
            raise ValidationError(_("Invalid JSON: %(error)s"), params={"error": str(exc)}) from exc

    def clean(self):
        cleaned_data = super().clean()
        org = self.instance.organization if (self.instance and self.instance.pk) else cleaned_data.get("organization")
        custom_fields = cleaned_data.get("custom_fields_json", {})
        if org and custom_fields:
            try:
                validate_custom_fields(org, custom_fields)
            except ValidationError as ve:
                self.add_error("custom_fields_json", ve)
        return cleaned_data


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    form = ContactAdminForm
    list_display = ["phone", "first_name", "last_name", "organization", "sms_status", "source", "created_at"]
    list_filter = ["organization", "sms_status", "source", "created_at"]
    search_fields = ["phone", "first_name", "last_name", "email"]
    readonly_fields = ["sms_status", "created_at", "updated_at"]
    list_select_related = ["organization"]

    def save_model(self, request, obj, form, change):
        data = {
            "phone": form.cleaned_data.get("phone"),
            "first_name": form.cleaned_data.get("first_name", ""),
            "last_name": form.cleaned_data.get("last_name", ""),
            "email": form.cleaned_data.get("email", ""),
            "custom_fields": form.cleaned_data.get("custom_fields_json", {}),
            "external_ids": form.cleaned_data.get("external_ids", {}),
        }

        if change and obj.pk:
            contact = update_contact(contact=obj, data=data)
            obj.pk = contact.pk
        else:
            source = form.cleaned_data.get("source") or Contact.Source.ADMIN
            contact, _created = save_contact(
                organization=form.cleaned_data.get("organization"),
                data=data,
                source=source,
            )
            obj.pk = contact.pk


class ContactListAdminForm(forms.ModelForm):
    class Meta:
        model = ContactList
        fields = ["organization", "name", "contacts"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["organization"].disabled = True
            self.fields["contacts"].queryset = Contact.objects.filter(
                organization=self.instance.organization
            )
        elif "organization" in self.data:
            try:
                org_id = int(self.data.get("organization"))
                self.fields["contacts"].queryset = Contact.objects.filter(organization_id=org_id)
            except (ValueError, TypeError):
                pass

    def clean(self):
        cleaned_data = super().clean()
        org = self.instance.organization if (self.instance and self.instance.pk) else cleaned_data.get("organization")
        contacts = cleaned_data.get("contacts")
        if org and contacts:
            mismatched = [c for c in contacts if c.organization_id != org.id]
            if mismatched:
                self.add_error(
                    "contacts",
                    ValidationError(_("All contacts in this list must belong to '%(org)s'."), params={"org": org.name})
                )
        return cleaned_data


@admin.register(ContactList)
class ContactListAdmin(admin.ModelAdmin):
    form = ContactListAdminForm
    list_display = ["name", "organization", "contact_count", "created_at"]
    list_filter = ["organization", "created_at"]
    search_fields = ["name"]
    filter_horizontal = ["contacts"]
    list_select_related = ["organization"]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(annotated_contact_count=Count("contacts"))

    def contact_count(self, obj):
        return getattr(obj, "annotated_contact_count", obj.contacts.count())
    contact_count.admin_order_field = "annotated_contact_count"
    contact_count.short_description = _("Contacts Count")


class ContactFieldAdminForm(forms.ModelForm):
    class Meta:
        model = ContactField
        fields = ["organization", "key", "label"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["key"].disabled = True
            self.fields["organization"].disabled = True


@admin.register(ContactField)
class ContactFieldAdmin(admin.ModelAdmin):
    form = ContactFieldAdminForm
    list_display = ["label", "key", "organization", "created_at"]
    list_filter = ["organization", "created_at"]
    search_fields = ["label", "key"]
    list_select_related = ["organization"]


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = [
        "filename", "organization", "total_rows", "created_count",
        "updated_count", "skipped_count", "consent_attested", "created_at"
    ]
    list_filter = ["organization", "consent_attested", "created_at"]
    search_fields = ["filename", "attestation_text"]
    readonly_fields = [
        "organization", "filename", "total_rows", "created_count",
        "updated_count", "skipped_count", "errors", "warnings",
        "consent_attested", "attestation_text", "contact_list", "created_at"
    ]
    list_select_related = ["organization", "contact_list"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
