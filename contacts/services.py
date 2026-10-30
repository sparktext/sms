import csv
import io
import difflib
import logging
import re
from typing import Dict, Any, List, Optional, Tuple, Set
from zoneinfo import available_timezones

from django.conf import settings
from django.db import transaction
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.translation import gettext_lazy as _

from .models import Contact, ContactField, ContactList, ImportBatch, RESERVED_FIELD_NAMES, KEY_REGEX
from .utils import normalize_phone

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 200
MAX_FIRST_NAME_LENGTH = 100
MAX_LAST_NAME_LENGTH = 100
MAX_EMAIL_LENGTH = 254
MAX_TIMEZONE_LENGTH = 64
MAX_CUSTOM_VALUE_LENGTH = 1000

MAX_FILE_SIZE_BYTES = getattr(settings, "CSV_IMPORT_MAX_FILE_SIZE", 10 * 1024 * 1024)  # 10 MB default
MAX_ROW_COUNT = getattr(settings, "CSV_IMPORT_MAX_ROWS", 50000)
MAX_REPORTED_ERRORS = getattr(settings, "CSV_IMPORT_MAX_REPORTED_ERRORS", 500)

ALL_TIMEZONES = available_timezones()


def _canonical_header(header: str) -> str:
    """Normalize string by removing spaces, underscores, and lowercasing."""
    return re.sub(r"[\s_]+", "", str(header).strip().lower())


def decode_csv_content(raw_bytes: bytes) -> Tuple[str, Optional[str]]:
    """
    Decodes CSV content:
    1. Try utf-8-sig (handles UTF-8 and UTF-8 with BOM).
    2. Fall back to cp1252.
    3. Return decoded string and warning if fallback was used.
    """
    if len(raw_bytes) > MAX_FILE_SIZE_BYTES:
        raise ValidationError(
            _("File size exceeds maximum permitted limit of %(max_mb)d MB."),
            params={"max_mb": MAX_FILE_SIZE_BYTES // (1024 * 1024)}
        )

    try:
        return raw_bytes.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        try:
            decoded = raw_bytes.decode("cp1252")
            return decoded, "File was decoded using Windows-1252 (cp1252) fallback because it was not valid UTF-8."
        except UnicodeDecodeError:
            raise ValidationError(_("Unable to decode CSV file. Please provide a UTF-8 or Windows-1252 encoded file."))


def validate_custom_fields(
    organization,
    custom_fields: Dict[str, Any],
    approved_keys: Optional[Set[str]] = None,
) -> None:
    """
    Shared validator for custom_fields dictionary.
    - Ensures it is a dict
    - Validates values are only str, int, float, bool, or None
    - Enforces max string length on values
    - Strictly checks keys against approved ContactField keys for the organization (can pass pre-fetched approved_keys)
    - Suggests closest match on typo
    """
    if not isinstance(custom_fields, dict):
        raise ValidationError(_("Custom fields must be a key-value dictionary."))

    if not custom_fields:
        return

    for key, val in custom_fields.items():
        if val is not None and not isinstance(val, (str, int, float, bool)):
            raise ValidationError(
                _("Custom field '%(key)s' has invalid value type '%(type)s'. Only string, number, and boolean are allowed.")
                % {"key": key, "type": type(val).__name__}
            )
        if isinstance(val, str) and len(val) > MAX_CUSTOM_VALUE_LENGTH:
            raise ValidationError(
                _("Custom field '%(key)s' exceeds maximum length of %(max)d characters.")
                % {"key": key, "max": MAX_CUSTOM_VALUE_LENGTH}
            )

    if approved_keys is None:
        approved_keys = set(
            ContactField.objects.filter(organization=organization).values_list("key", flat=True)
        )

    for key in custom_fields.keys():
        if key not in approved_keys:
            closest_matches = difflib.get_close_matches(key, approved_keys, n=1, cutoff=0.6)
            if closest_matches:
                suggestion = closest_matches[0]
                msg = _(
                    "Unknown custom field '%(key)s'. Did you mean '%(suggestion)s'?"
                ) % {"key": key, "suggestion": suggestion}
            else:
                msg = _("Unknown custom field '%(key)s'. This field is not approved for this business.") % {"key": key}
            raise ValidationError(msg)


def _validate_contact_attributes(data: Dict[str, Any]) -> None:
    """
    Validates first_name, last_name, email, timezone length and formats.
    """
    first_name = data.get("first_name")
    if first_name is not None:
        if not isinstance(first_name, str):
            raise ValidationError({"first_name": _("First name must be a string.")})
        if len(first_name) > MAX_FIRST_NAME_LENGTH:
            raise ValidationError({"first_name": _("First name cannot exceed %(max)d characters.") % {"max": MAX_FIRST_NAME_LENGTH}})

    last_name = data.get("last_name")
    if last_name is not None:
        if not isinstance(last_name, str):
            raise ValidationError({"last_name": _("Last name must be a string.")})
        if len(last_name) > MAX_LAST_NAME_LENGTH:
            raise ValidationError({"last_name": _("Last name cannot exceed %(max)d characters.") % {"max": MAX_LAST_NAME_LENGTH}})

    email = data.get("email")
    if email:
        if not isinstance(email, str):
            raise ValidationError({"email": _("Email must be a string.")})
        if len(email) > MAX_EMAIL_LENGTH:
            raise ValidationError({"email": _("Email cannot exceed %(max)d characters.") % {"max": MAX_EMAIL_LENGTH}})
        try:
            validate_email(email.strip())
        except ValidationError:
            raise ValidationError({"email": _("Enter a valid email address.")})

    tz = data.get("timezone")
    if tz:
        if not isinstance(tz, str):
            raise ValidationError({"timezone": _("Timezone must be a string.")})
        if len(tz) > MAX_TIMEZONE_LENGTH:
            raise ValidationError({"timezone": _("Timezone cannot exceed %(max)d characters.") % {"max": MAX_TIMEZONE_LENGTH}})
        if tz.strip() not in ALL_TIMEZONES:
            raise ValidationError({"timezone": _("'%(tz)s' is not a valid IANA timezone (e.g. 'America/Los_Angeles').") % {"tz": tz}})


def save_contact(
    organization,
    data: Dict[str, Any],
    source: str = Contact.Source.MANUAL,
    region: Optional[str] = None,
    approved_keys: Optional[Set[str]] = None,
) -> Tuple[Contact, bool]:
    """
    Unified entry point for creating or updating a contact by (organization, phone).
    Accepts pre-fetched approved_keys so batch imports don't re-query ContactField once per row.
    """
    raw_phone = data.get("phone")
    if not raw_phone:
        raise ValidationError({"phone": _("Phone number is required.")})

    normalized_phone = normalize_phone(raw_phone, default_region=region)
    _validate_contact_attributes(data)

    custom_fields = data.get("custom_fields", {})
    validate_custom_fields(organization, custom_fields, approved_keys=approved_keys)

    external_ids = data.get("external_ids", {})
    if not isinstance(external_ids, dict):
        raise ValidationError({"external_ids": _("External IDs must be a key-value dictionary.")})

    first_name = data.get("first_name", "")
    last_name = data.get("last_name", "")
    email = data.get("email", "")
    timezone = data.get("timezone", "")

    with transaction.atomic():
        contact, created = Contact.objects.select_for_update().get_or_create(
            organization=organization,
            phone=normalized_phone,
            defaults={
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "timezone": timezone,
                "source": source,
                "custom_fields": custom_fields,
                "external_ids": external_ids,
                "sms_status": Contact.SMSStatus.UNKNOWN,
            },
        )

        if not created:
            # Overwrite only with non-empty values; preserve source and sms_status
            if "first_name" in data and data["first_name"]:
                contact.first_name = data["first_name"]
            if "last_name" in data and data["last_name"]:
                contact.last_name = data["last_name"]
            if "email" in data and data["email"]:
                contact.email = data["email"]
            if "timezone" in data and data["timezone"]:
                contact.timezone = data["timezone"]
            if "custom_fields" in data and data["custom_fields"]:
                updated_custom = dict(contact.custom_fields)
                for k, v in data["custom_fields"].items():
                    if v is not None and v != "":
                        updated_custom[k] = v
                contact.custom_fields = updated_custom
            if "external_ids" in data and data["external_ids"]:
                updated_ext = dict(contact.external_ids)
                updated_ext.update(data["external_ids"])
                contact.external_ids = updated_ext

            contact.save()

    return contact, created


def update_contact(
    contact: Contact,
    data: Dict[str, Any],
    region: Optional[str] = None,
) -> Contact:
    """
    Dedicated function for editing an existing Contact in-place without duplicating.
    """
    target_org = data.get("organization", contact.organization)
    if target_org != contact.organization:
        raise ValidationError({"organization": _("Organization cannot be changed on an existing contact.")})

    raw_phone = data.get("phone", contact.phone)
    if not raw_phone:
        raise ValidationError({"phone": _("Phone number is required.")})

    normalized_phone = normalize_phone(raw_phone, default_region=region)

    if normalized_phone != contact.phone:
        conflict = Contact.objects.filter(
            organization=contact.organization,
            phone=normalized_phone,
        ).exclude(pk=contact.pk).exists()
        if conflict:
            raise ValidationError({
                "phone": _("Another contact with phone '%(phone)s' already exists in this business.") % {"phone": normalized_phone}
            })
        contact.phone = normalized_phone

    _validate_contact_attributes(data)

    if "first_name" in data:
        contact.first_name = data["first_name"]
    if "last_name" in data:
        contact.last_name = data["last_name"]
    if "email" in data:
        contact.email = data["email"]
    if "timezone" in data:
        contact.timezone = data["timezone"]

    if "custom_fields" in data:
        custom_fields = data["custom_fields"]
        validate_custom_fields(contact.organization, custom_fields)
        contact.custom_fields = custom_fields

    if "external_ids" in data:
        external_ids = data["external_ids"]
        if not isinstance(external_ids, dict):
            raise ValidationError({"external_ids": _("External IDs must be a key-value dictionary.")})
        contact.external_ids = external_ids

    contact.save()
    return contact


# =========================================================================
# CSV Import Logic
# =========================================================================

def preview_csv(file_or_content, sample_size: int = 5) -> Dict[str, Any]:
    """
    Step 1 (Preview): Parse the CSV file/stream and return column names
    along with sample rows and warnings.
    Keeps blank headers and flags duplicates.
    """
    warning = None
    if isinstance(file_or_content, bytes):
        content, warning = decode_csv_content(file_or_content)
    elif isinstance(file_or_content, str):
        content = file_or_content
    else:
        raw = file_or_content.read()
        if isinstance(raw, bytes):
            content, warning = decode_csv_content(raw)
        else:
            content = raw

    reader = csv.reader(io.StringIO(content))
    try:
        raw_headers = next(reader)
    except StopIteration:
        return {"headers": [], "rows": [], "duplicate_headers": [], "total_rows_sampled": 0, "warning": warning}

    headers = [h.strip() for h in raw_headers]

    seen_headers = set()
    duplicate_headers = []
    for h in headers:
        if h in seen_headers and h not in duplicate_headers:
            duplicate_headers.append(h)
        seen_headers.add(h)

    sample_rows = []
    for _idx in range(sample_size):
        try:
            row = next(reader)
            if any(cell.strip() for cell in row):
                sample_rows.append([cell.strip() for cell in row[:len(headers)]])
        except StopIteration:
            break

    return {
        "headers": headers,
        "rows": sample_rows,
        "duplicate_headers": duplicate_headers,
        "total_rows_sampled": len(sample_rows),
        "warning": warning,
    }


def suggest_column_mappings(organization, headers: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Auto-suggest a mapping when a column header matches an existing
    ContactField key or label (case-insensitive, ignoring spaces and underscores).
    Recognizes standard core fields: phone, name, email, timezone.
    """
    core_fields = {
        "phone": ["phone", "phonenumber", "mobile", "cell", "tel", "telephone"],
        "first_name": ["firstname", "first", "fname", "givenname"],
        "last_name": ["lastname", "last", "lname", "surname", "familyname"],
        "name": ["name", "fullname", "contactname", "customername"],
        "email": ["email", "emailaddress"],
        "timezone": ["timezone", "tz"],
    }

    contact_fields = list(ContactField.objects.filter(organization=organization))
    suggestions: Dict[str, Dict[str, Any]] = {}

    for header in headers:
        if not header.strip():
            suggestions[header] = {"type": "blank", "target": None, "decision": "skip"}
            continue

        canon = _canonical_header(header)
        matched = False

        for field_name, aliases in core_fields.items():
            if canon in aliases:
                suggestions[header] = {
                    "type": "core",
                    "target": field_name,
                    "decision": "map",
                }
                matched = True
                break
        if matched:
            continue

        for cf in contact_fields:
            if canon == _canonical_header(cf.key) or canon == _canonical_header(cf.label):
                suggestions[header] = {
                    "type": "custom",
                    "target": cf.key,
                    "decision": "map",
                }
                matched = True
                break
        if matched:
            continue

        suggestions[header] = {
            "type": "unmapped",
            "target": None,
            "decision": "skip",
        }

    return suggestions


def validate_csv_mapping(organization, headers: List[str], column_mapping: Dict[str, Dict[str, Any]]) -> None:
    """
    Validates the entire mapping UP FRONT before touching rows or creating fields.
    """
    phone_targets = 0
    approved_keys = set(ContactField.objects.filter(organization=organization).values_list("key", flat=True))
    core_targets = {"phone", "first_name", "last_name", "name", "email", "timezone"}
    added_keys = set()

    for header, mapping in column_mapping.items():
        decision = mapping.get("decision", "skip")
        target = mapping.get("target")

        if decision == "map":
            if not target:
                raise ValidationError(_("Column '%(header)s' has decision 'map' but no target specified.") % {"header": header})
            if target == "phone":
                phone_targets += 1
            elif target in core_targets:
                pass
            elif target not in approved_keys:
                raise ValidationError(_("Target '%(target)s' for column '%(header)s' is not an approved field.") % {"target": target, "header": header})

        elif decision == "add":
            target_key = mapping.get("target") or re.sub(r"[^a-z0-9_]+", "_", header.strip().lower()).strip("_")
            if not target_key:
                raise ValidationError(_("Target key for column '%(header)s' cannot be empty.") % {"header": header})

            target_key = target_key.strip().lower()
            if not KEY_REGEX.match(target_key):
                raise ValidationError(
                    _("Invalid key '%(key)s' for column '%(header)s'. Key must start with a letter and contain only lowercase letters, digits, and underscores.")
                    % {"key": target_key, "header": header}
                )

            if target_key in RESERVED_FIELD_NAMES:
                raise ValidationError(
                    _("Cannot add custom field '%(key)s' because it is a reserved field name.") % {"key": target_key}
                )

            if target_key in added_keys:
                raise ValidationError(
                    _("Duplicate custom field key '%(key)s' specified across multiple 'add' columns.") % {"key": target_key}
                )
            added_keys.add(target_key)

    if phone_targets == 0:
        raise ValidationError(_("CSV mapping must include exactly one 'phone' column."))
    if phone_targets > 1:
        raise ValidationError(_("CSV mapping cannot map more than one column to 'phone'."))


def import_contacts_csv(
    organization,
    file_or_content,
    column_mapping: Dict[str, Dict[str, str]],
    contact_list: Optional[ContactList] = None,
    consent_attestation: bool = False,
    attestation_text: str = "",
    filename: str = "upload.csv",
    batch_size: int = 100,
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Step 3 (Import):
    - Validates mapping upfront
    - Decodes with utf-8-sig or cp1252 fallback
    - Enforces row caps and detects in-file duplicate phones
    - Optional contact_list addition
    - Creates an ImportBatch record for audit logging
    - If consent_attestation is True, records compliance.services.record_consent with source 'csv_attestation'
    - Only overwrites non-empty values
    - Sets source on creation only
    - Returns report with warnings, row errors, and import_batch_id
    """
    if contact_list and contact_list.organization_id != organization.id:
        raise ValidationError(_("The provided contact_list does not belong to this organization."))

    warning = None
    if isinstance(file_or_content, bytes):
        content, warning = decode_csv_content(file_or_content)
    elif isinstance(file_or_content, str):
        content = file_or_content
    else:
        raw = file_or_content.read()
        if isinstance(raw, bytes):
            content, warning = decode_csv_content(raw)
        else:
            content = raw

    reader = csv.reader(io.StringIO(content))
    try:
        raw_headers = next(reader)
    except StopIteration:
        return {"created": 0, "updated": 0, "skipped": 0, "errors": [{"row": 1, "error": "CSV file is empty."}], "warning": warning}

    headers = [h.strip() for h in raw_headers]
    validate_csv_mapping(organization, headers, column_mapping)

    # Handle "add" decisions: Create new ContactFields upfront
    resolved_custom_fields = {}
    for header, map_info in column_mapping.items():
        decision = map_info.get("decision")
        if decision == "add":
            target_key = map_info.get("target") or re.sub(r"[^a-z0-9_]+", "_", header.strip().lower()).strip("_")
            target_key = target_key.strip().lower()
            target_label = map_info.get("label") or header.strip() or target_key
            field_obj, _created = ContactField.objects.get_or_create(
                organization=organization,
                key=target_key,
                defaults={"label": target_label},
            )
            resolved_custom_fields[header] = field_obj.key
        elif decision == "map":
            target_key = map_info.get("target")
            resolved_custom_fields[header] = target_key

    # Cache approved keys once for the entire import
    approved_keys = set(ContactField.objects.filter(organization=organization).values_list("key", flat=True))

    created_count = 0
    updated_count = 0
    skipped_count = 0
    errors: List[Dict[str, Any]] = []

    # Read rows and check row cap
    rows = []
    seen_in_file_phones: Set[str] = set()

    for line_idx, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(rows) >= MAX_ROW_COUNT:
            raise ValidationError(
                _("CSV file exceeds the maximum allowed limit of %(max)d rows.") % {"max": MAX_ROW_COUNT}
            )
        rows.append((line_idx, row))

    processed_contacts: List[Contact] = []

    # Process in transactional batches
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        with transaction.atomic():
            for line_idx, row in batch:
                row_dict = {}
                for col_idx, cell_value in enumerate(row):
                    if col_idx < len(headers):
                        col_header = headers[col_idx]
                        row_dict[col_header] = cell_value.strip()

                phone_val = None
                contact_data = {
                    "custom_fields": {},
                    "external_ids": {},
                }

                for header, cell_value in row_dict.items():
                    mapping = column_mapping.get(header, {})
                    decision = mapping.get("decision", "skip")

                    if decision == "skip":
                        continue

                    target = mapping.get("target")
                    if target == "phone":
                        phone_val = cell_value
                    elif target == "first_name":
                        contact_data["first_name"] = cell_value
                    elif target == "last_name":
                        contact_data["last_name"] = cell_value
                    elif target == "name":
                        parts = cell_value.split(maxsplit=1)
                        if parts:
                            contact_data["first_name"] = parts[0]
                            if len(parts) > 1:
                                contact_data["last_name"] = parts[1]
                    elif target == "email":
                        contact_data["email"] = cell_value
                    elif target == "timezone":
                        contact_data["timezone"] = cell_value
                    elif decision in ("map", "add"):
                        field_key = resolved_custom_fields.get(header) or target
                        if cell_value:
                            contact_data["custom_fields"][field_key] = cell_value

                if not phone_val:
                    if len(errors) < MAX_REPORTED_ERRORS:
                        errors.append({"row": line_idx, "error": "Missing phone number in row."})
                    skipped_count += 1
                    continue

                contact_data["phone"] = phone_val

                # Pre-normalize to check in-file duplicate phone
                try:
                    norm_phone = normalize_phone(phone_val, default_region=region)
                    if norm_phone in seen_in_file_phones:
                        if len(errors) < MAX_REPORTED_ERRORS:
                            errors.append({
                                "row": line_idx,
                                "error": f"Duplicate phone number '{norm_phone}' found earlier within this file."
                            })
                        skipped_count += 1
                        continue
                    seen_in_file_phones.add(norm_phone)
                except ValidationError as ve:
                    err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
                    if len(errors) < MAX_REPORTED_ERRORS:
                        errors.append({"row": line_idx, "error": err_msg})
                    skipped_count += 1
                    continue

                try:
                    # Save contact using cached approved_keys
                    with transaction.atomic():
                        contact, created = save_contact(
                            organization=organization,
                            data=contact_data,
                            source=Contact.Source.CSV,
                            region=region,
                            approved_keys=approved_keys,
                        )
                        if created:
                            created_count += 1
                        else:
                            updated_count += 1
                        processed_contacts.append(contact)

                        # Handle consent attestation if requested
                        if consent_attestation:
                            from compliance.services import record_consent
                            from compliance.models import ConsentEvent
                            proof = attestation_text.strip() or f"CSV Import Attestation for {filename}"
                            record_consent(
                                contact=contact,
                                event_type=ConsentEvent.EventType.OPT_IN,
                                source=ConsentEvent.Source.CSV_ATTESTATION,
                                proof_text=proof,
                            )
                except ValidationError as ve:
                    err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
                    if len(errors) < MAX_REPORTED_ERRORS:
                        errors.append({"row": line_idx, "error": err_msg})
                    skipped_count += 1
                except Exception as exc:
                    logger.exception(f"Unexpected error importing row {line_idx} for organization {organization.id}")
                    if len(errors) < MAX_REPORTED_ERRORS:
                        errors.append({"row": line_idx, "error": "A system error occurred while processing this row."})
                    skipped_count += 1

    # Associate processed contacts with contact_list if requested
    if contact_list and processed_contacts:
        contact_list.contacts.add(*processed_contacts)

    warnings_list = [warning] if warning else []

    # Create ImportBatch record for audit tracking
    import_batch = ImportBatch.objects.create(
        organization=organization,
        filename=filename,
        total_rows=len(rows),
        created_count=created_count,
        updated_count=updated_count,
        skipped_count=skipped_count,
        errors=errors,
        warnings=warnings_list,
        consent_attested=consent_attestation,
        attestation_text=attestation_text,
        contact_list=contact_list,
    )

    return {
        "import_batch_id": import_batch.id,
        "created": created_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "errors": errors,
        "warning": warning,
    }
