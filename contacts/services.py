import csv
import io
import difflib
import logging
import re
from typing import Dict, Any, List, Optional, Tuple, Set

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
MAX_CUSTOM_VALUE_LENGTH = 5000

MAX_FILE_SIZE_BYTES = getattr(settings, "CSV_IMPORT_MAX_FILE_SIZE", 10 * 1024 * 1024)  # 10 MB default
MAX_ROW_COUNT = getattr(settings, "CSV_IMPORT_MAX_ROWS", 50000)
MAX_REPORTED_ERRORS = getattr(settings, "CSV_IMPORT_MAX_REPORTED_ERRORS", 500)


def _canonical_header(header: str) -> str:
    """Normalize string by removing spaces, underscores, hyphens and lowercasing."""
    return re.sub(r"[\s_\-]+", "", str(header).strip().lower())


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
    Validates first_name, last_name and email length and formats.
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

    source_val = data.get("source") or data.get("vendor")
    if source_val is not None:
        if not isinstance(source_val, str):
            raise ValidationError({"source": _("Source must be a string.")})
        if len(source_val) > 100:
            raise ValidationError({"source": _("Source cannot exceed 100 characters.")})


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

    # Priority: explicit source/vendor in data > source kwarg (if non-default) > "Direct"
    data_source = (data.get("source") or data.get("vendor") or "").strip()
    if data_source:
        resolved_source = data_source
    elif source and source != Contact.Source.MANUAL:
        resolved_source = source
    else:
        resolved_source = "Direct"

    with transaction.atomic():
        contact, created = Contact.objects.select_for_update().get_or_create(
            organization=organization,
            phone=normalized_phone,
            defaults={
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "source": resolved_source,
                "custom_fields": custom_fields,
                "external_ids": external_ids,
                "sms_status": Contact.SMSStatus.UNKNOWN,
            },
        )

        if not created:
            # Overwrite only with non-empty values; preserve source and sms_status unless explicitly provided
            if "first_name" in data and data["first_name"]:
                contact.first_name = data["first_name"]
            if "last_name" in data and data["last_name"]:
                contact.last_name = data["last_name"]
            if "email" in data and data["email"]:
                contact.email = data["email"]
            source_update = (data.get("source") or data.get("vendor") or "").strip()
            if source_update:
                contact.source = source_update
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
    source_update = (data.get("source") or data.get("vendor") or "").strip()
    if source_update:
        contact.source = source_update

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

_US_TEXT_DATE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")


def _us_format_spreadsheet_rows(raw_rows: List[List[Any]], width: int) -> List[List[str]]:
    """
    Convert spreadsheet cell values to strings for a US audience.

    Real date cells are written as MM/DD/YYYY (plus HH:MM when there is a time),
    instead of Python's ISO "1965-11-12 00:00:00".

    It also repairs a common Excel problem: a file of US dates (MM/DD/YYYY) opened
    on a computer set to day/month order. Excel then turns "12/11/1965" (Dec 11)
    into the date 12 Nov 1965 and leaves "12/25/1965" as text, because there is
    no month 25. When a column shows that signature (real dates mixed with text
    dates whose second number is above 12, and no real date with a day above 12),
    the converted dates are swapped back to their intended US month/day.
    """
    import datetime as _dt

    swapped_cols = set()
    for col in range(width):
        has_dates = has_us_text_gt12 = has_date_day_gt12 = False
        for row in raw_rows:
            v = row[col] if col < len(row) else None
            if isinstance(v, (_dt.datetime, _dt.date)):
                has_dates = True
                if v.day > 12:
                    has_date_day_gt12 = True
            elif isinstance(v, str):
                m = _US_TEXT_DATE.match(v)
                if m and int(m.group(2)) > 12:
                    has_us_text_gt12 = True
        if has_dates and has_us_text_gt12 and not has_date_day_gt12:
            swapped_cols.add(col)

    def fmt(v, col):
        if v is None:
            return ""
        if isinstance(v, (_dt.datetime, _dt.date)):
            month, day = (v.day, v.month) if col in swapped_cols else (v.month, v.day)
            out = f"{month:02d}/{day:02d}/{v.year:04d}"
            if isinstance(v, _dt.datetime) and (v.hour or v.minute):
                out += f" {v.hour:02d}:{v.minute:02d}"
            return out
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v).strip()

    return [[fmt(row[c] if c < len(row) else None, c) for c in range(width)] for row in raw_rows]


def extract_tabular_data(
    file_or_content,
    filename: Optional[str] = None,
) -> Tuple[List[str], Any, Optional[str]]:
    """
    Parses CSV, XLSX, or XLS files into (headers, rows_generator, warning).
    Supports:
      - CSV (.csv): utf-8, utf-8-sig, and cp1252 fallback
      - Excel (.xlsx): openpyxl
      - Excel (.xls): xlrd
    """
    raw_bytes = None

    if isinstance(file_or_content, bytes):
        raw_bytes = file_or_content
    elif isinstance(file_or_content, str):
        reader = csv.reader(io.StringIO(file_or_content))
        try:
            raw_headers = next(reader)
        except StopIteration:
            return [], iter([]), None
        headers = [h.strip() for h in raw_headers]
        def str_csv_rows():
            for r in reader:
                if any(c.strip() for c in r):
                    yield [c.strip() for c in r[:len(headers)]]
        return headers, str_csv_rows(), None
    else:
        raw = file_or_content.read()
        if isinstance(raw, bytes):
            raw_bytes = raw
        else:
            reader = csv.reader(io.StringIO(raw))
            try:
                raw_headers = next(reader)
            except StopIteration:
                return [], iter([]), None
            headers = [h.strip() for h in raw_headers]
            def raw_str_rows():
                for r in reader:
                    if any(c.strip() for c in r):
                        yield [c.strip() for c in r[:len(headers)]]
            return headers, raw_str_rows(), None

    if len(raw_bytes) > MAX_FILE_SIZE_BYTES:
        raise ValidationError(
            _("File size exceeds maximum permitted limit of %(max_mb)d MB."),
            params={"max_mb": MAX_FILE_SIZE_BYTES // (1024 * 1024)}
        )

    fname = (filename or "").lower().strip()
    is_xlsx = fname.endswith(".xlsx") or raw_bytes.startswith(b"PK\x03\x04")
    is_xls = fname.endswith(".xls") or raw_bytes.startswith(b"\xd0\xcf\x11\xe0")

    if is_xlsx:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
            sheet = wb.active
            if sheet is None:
                return [], iter([]), None

            def _cell_to_str(val):
                if val is None:
                    return ""
                if isinstance(val, float) and val.is_integer():
                    return str(int(val))
                return str(val).strip()

            row_iter = sheet.iter_rows(values_only=True)
            first_row = None
            for candidate in row_iter:
                if any(v is not None and str(v).strip() for v in candidate):
                    first_row = candidate
                    break
            if first_row is None:
                return [], iter([]), None

            headers = [_cell_to_str(h) for h in first_row]

            # Read every row first so date columns can be checked as a whole (see _us_format_spreadsheet_rows)
            raw_rows = [
                list(r[:len(headers)])
                for r in row_iter
                if any(v is not None and str(v).strip() for v in r)
            ]
            return headers, iter(_us_format_spreadsheet_rows(raw_rows, len(headers))), None
        except Exception as e:
            if fname.endswith(".xlsx"):
                raise ValidationError(_("Could not read Excel file (.xlsx): %(error)s") % {"error": str(e)})

    if is_xls:
        try:
            import xlrd
            wb = xlrd.open_workbook(file_contents=raw_bytes)
            sheet = wb.sheet_by_index(0)
            if sheet.nrows == 0:
                return [], iter([]), None

            def _xlrd_cell_to_str(cell_value, cell_type):
                if cell_value is None or cell_type == xlrd.XL_CELL_EMPTY:
                    return ""
                if cell_type == xlrd.XL_CELL_NUMBER and isinstance(cell_value, float) and cell_value.is_integer():
                    return str(int(cell_value))
                return str(cell_value).strip()

            headers = [_xlrd_cell_to_str(sheet.cell_value(0, c), sheet.cell_type(0, c)) for c in range(sheet.ncols)]

            def _xlrd_raw(r, c):
                ctype = sheet.cell_type(r, c)
                if ctype == xlrd.XL_CELL_DATE:
                    try:
                        return xlrd.xldate_as_datetime(sheet.cell_value(r, c), wb.datemode)
                    except Exception:
                        pass
                return _xlrd_cell_to_str(sheet.cell_value(r, c), ctype)

            raw_rows = []
            for r in range(1, sheet.nrows):
                row_vals = [_xlrd_raw(r, c) for c in range(len(headers))]
                if any(v not in ("", None) for v in row_vals):
                    raw_rows.append(row_vals)
            return headers, iter(_us_format_spreadsheet_rows(raw_rows, len(headers))), None
        except Exception as e:
            if fname.endswith(".xls"):
                raise ValidationError(_("Could not read Excel file (.xls): %(error)s") % {"error": str(e)})

    # Default to CSV decoding
    content, warning = decode_csv_content(raw_bytes)
    reader = csv.reader(io.StringIO(content))
    try:
        raw_headers = next(reader)
    except StopIteration:
        return [], iter([]), warning

    headers = [h.strip() for h in raw_headers]

    def csv_rows_gen():
        for r in reader:
            if any(cell.strip() for cell in r):
                yield [cell.strip() for cell in r[:len(headers)]]

    return headers, csv_rows_gen(), warning


def preview_csv(file_or_content, sample_size: int = 5, filename: Optional[str] = None) -> Dict[str, Any]:
    """
    Step 1 (Preview): Parse the CSV/Excel file and return column names
    along with sample rows and warnings.
    Keeps blank headers and flags duplicates.
    """
    headers, row_gen, warning = extract_tabular_data(file_or_content, filename=filename)

    seen_headers = set()
    duplicate_headers = []
    for h in headers:
        if h in seen_headers and h not in duplicate_headers:
            duplicate_headers.append(h)
        seen_headers.add(h)

    sample_rows = []
    for _idx in range(sample_size):
        try:
            row = next(row_gen)
            sample_rows.append(row)
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
    Recognizes standard core fields: phone, name, email, source.
    """
    core_fields = {
        "phone": ["phone", "phonenumber", "mobile", "cell", "tel", "telephone"],
        "source": ["source", "leadsource", "lead_source", "vendor", "leadvendor", "sourcevendor", "provider", "channel"],
        "vendor": ["vendor", "leadvendor", "sourcevendor", "provider", "leadsource", "source"],
        "first_name": ["firstname", "first", "fname", "givenname"],
        "last_name": ["lastname", "last", "lname", "surname", "familyname"],
        "name": ["name", "fullname", "contactname", "customername"],
        "email": ["email", "emailaddress"],
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
    core_targets = {"phone", "first_name", "last_name", "name", "email", "source", "vendor"}
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
    default_source: str = "Direct",
    default_vendor: Optional[str] = None,
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

    headers, row_gen, warning = extract_tabular_data(file_or_content, filename=filename)
    if not headers:
        return {"created": 0, "updated": 0, "skipped": 0, "errors": [{"row": 1, "error": "File is empty or has no header row."}], "warning": warning}

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

    for line_idx, row in enumerate(row_gen, start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(rows) >= MAX_ROW_COUNT:
            raise ValidationError(
                _("File exceeds the maximum allowed limit of %(max)d rows.") % {"max": MAX_ROW_COUNT}
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
                    elif target in ("source", "vendor"):
                        contact_data["source"] = cell_value
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
                if not contact_data.get("source"):
                    contact_data["source"] = (default_source or default_vendor or "").strip() or "Direct"

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


def _flatten_dict(d: Dict[str, Any], prefix: str = "") -> Dict[str, str]:
    """
    Recursively flatten a nested dict into snake_case keys with scalar string values.

    Example:
        {"person": {"firstName": "Jane", "address": {"city": "LA"}}}
        → {"person_first_name": "Jane", "person_address_city": "LA"}

    Lists are stringified. Null values are skipped.
    """
    items: Dict[str, str] = {}
    for k, v in d.items():
        # Convert camelCase to snake_case for the key segment
        seg = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(k)).lower()
        seg = re.sub(r"[^a-z0-9_]", "_", seg)
        seg = re.sub(r"_+", "_", seg).strip("_")
        full_key = f"{prefix}_{seg}" if prefix else seg
        if v is None:
            continue
        elif isinstance(v, dict):
            items.update(_flatten_dict(v, full_key))
        elif isinstance(v, list):
            # Stringify short lists; skip empty ones
            str_vals = [str(x) for x in v if x is not None]
            if str_vals:
                items[full_key] = ", ".join(str_vals)
        else:
            items[full_key] = str(v).strip()
    return items


# Well-known nested payload shapes from lead vendors (e.g. insurance, real-estate)
_PERSON_PHONE_KEYS = {"phone", "mobile", "cell", "tel", "contact_phone", "phone_number"}
_PERSON_FIRST_NAME_KEYS = {"firstname", "first_name"}
_PERSON_LAST_NAME_KEYS = {"lastname", "last_name"}
_PERSON_EMAIL_KEYS = {"email", "e_mail"}
_PERSON_NAME_KEYS = {"name", "full_name", "contact_name"}


def _extract_person_fields(raw_dict: Dict[str, Any]):
    """
    Look for a 'person' (or similar) sub-object and extract core contact fields
    (phone, first_name, last_name, email) from it. Returns extracted values and
    the remaining nested dict for flattening into custom fields.
    """
    phone = ""
    first_name = ""
    last_name = ""
    email = ""

    person_keys = ["person", "contact", "lead", "customer", "subscriber"]
    person_obj = None
    for pk in person_keys:
        candidate = raw_dict.get(pk)
        if isinstance(candidate, dict):
            person_obj = candidate
            break

    if not person_obj:
        return phone, first_name, last_name, email

    # Normalize keys to lower for matching
    lower_map = {k.lower().replace("-", "_"): k for k in person_obj}

    for pkey in _PERSON_PHONE_KEYS:
        orig = lower_map.get(pkey)
        if orig and person_obj.get(orig):
            phone = str(person_obj[orig]).strip()
            break

    for pkey in _PERSON_FIRST_NAME_KEYS:
        orig = lower_map.get(pkey)
        if orig and person_obj.get(orig):
            first_name = str(person_obj[orig]).strip()
            break

    for pkey in _PERSON_LAST_NAME_KEYS:
        orig = lower_map.get(pkey)
        if orig and person_obj.get(orig):
            last_name = str(person_obj[orig]).strip()
            break

    if not first_name and not last_name:
        for pkey in _PERSON_NAME_KEYS:
            orig = lower_map.get(pkey)
            if orig and person_obj.get(orig):
                parts = str(person_obj[orig]).strip().split()
                first_name = parts[0]
                if len(parts) > 1:
                    last_name = " ".join(parts[1:])
                break

    for pkey in _PERSON_EMAIL_KEYS:
        orig = lower_map.get(pkey)
        if orig and person_obj.get(orig):
            email = str(person_obj[orig]).strip()
            break

    return phone, first_name, last_name, email


def ingest_lead_data(
    organization,
    raw_dict: Dict[str, Any],
    default_list: Optional[ContactList] = None,
    source: str = Contact.Source.API,
    default_consent: bool = True,
    region: Optional[str] = None,
) -> Tuple[Contact, bool]:
    """
    Ingests a single lead dictionary from external API, webhook, or remote feed.
    Parses flexible field names (phone, phone_number, mobile, full_name, etc.),
    auto-flattens nested JSON structures (person, homePolicy, autoPolicy, etc.)
    into proper custom field columns, auto-registers custom fields, handles list
    assignment, and records TCPA opt-in consent.
    """
    # -------------------------------------------------------------------
    # 1) Extract core contact fields from nested "person" / "contact" objects
    # -------------------------------------------------------------------
    nested_phone, nested_fn, nested_ln, nested_email = _extract_person_fields(raw_dict)

    # -------------------------------------------------------------------
    # 2) Top-level core field extraction (takes priority over nested)
    # -------------------------------------------------------------------
    phone_keys = ["phone", "phone_number", "mobile", "cell", "tel", "contact_phone"]
    raw_phone = ""
    for k in phone_keys:
        val = raw_dict.get(k)
        if val is not None and str(val).strip():
            raw_phone = str(val).strip()
            break
    if not raw_phone:
        raw_phone = nested_phone
    if not raw_phone:
        raise ValidationError({"phone": _("Phone number is required.")})

    # First and last name extraction
    first_name = str(raw_dict.get("first_name", "")).strip()
    last_name = str(raw_dict.get("last_name", "")).strip()
    if not first_name and not last_name:
        for name_key in ["name", "full_name", "contact_name"]:
            val = str(raw_dict.get(name_key, "")).strip()
            if val:
                parts = val.split()
                first_name = parts[0]
                if len(parts) > 1:
                    last_name = " ".join(parts[1:])
                break
    # Fallback to nested person object fields
    if not first_name:
        first_name = nested_fn
    if not last_name:
        last_name = nested_ln

    email = str(raw_dict.get("email", "")).strip() or nested_email

    # -------------------------------------------------------------------
    # 3) Custom fields collection
    # -------------------------------------------------------------------
    custom_fields = {}
    if isinstance(raw_dict.get("custom_fields"), dict):
        custom_fields.update(raw_dict["custom_fields"])

    for stage_key in ["stage", "lead_stage", "crm_stage"]:
        if raw_dict.get(stage_key):
            custom_fields["stage"] = str(raw_dict[stage_key]).strip()
            break

    for form_key in ["form", "form_name", "source_name", "campaign_name"]:
        if raw_dict.get(form_key):
            custom_fields["form"] = str(raw_dict[form_key]).strip()
            break

    reserved_top_level = {
        "phone", "phone_number", "mobile", "cell", "tel", "contact_phone",
        "first_name", "last_name", "name", "full_name", "contact_name",
        "email", "timezone", "list", "list_name", "list_id", "lists",
        "consent", "opt_in", "api_key", "custom_fields", "external_ids",
        "source", "stage", "lead_stage", "crm_stage", "form", "form_name",
        "source_name", "campaign_name",
    }

    # Fields to skip when flattening nested person object (already extracted)
    _person_extracted_keys = (
        _PERSON_PHONE_KEYS | _PERSON_FIRST_NAME_KEYS |
        _PERSON_LAST_NAME_KEYS | _PERSON_EMAIL_KEYS | _PERSON_NAME_KEYS
    )

    for k, v in raw_dict.items():
        if k in reserved_top_level or v is None:
            continue

        if isinstance(v, dict):
            # Flatten nested objects into prefixed custom fields
            # For "person", skip already-extracted core fields
            is_person_obj = k.lower() in ("person", "contact", "lead", "customer", "subscriber")
            flattened = _flatten_dict(v, prefix=k.lower() if not is_person_obj else "")
            for fk, fv in flattened.items():
                if is_person_obj:
                    # Skip keys that were already extracted as core fields
                    fk_lower = fk.lower().replace("-", "_")
                    if fk_lower in _person_extracted_keys:
                        continue
                clean_fk = re.sub(r"[^a-zA-Z0-9_]", "_", fk.strip().lower())
                clean_fk = re.sub(r"^[^a-zA-Z]+", "", clean_fk)
                clean_fk = re.sub(r"_+", "_", clean_fk).strip("_")
                if clean_fk and clean_fk not in RESERVED_FIELD_NAMES and len(clean_fk) <= 64:
                    custom_fields[clean_fk] = fv
        elif isinstance(v, list):
            str_vals = [str(x) for x in v if x is not None and not isinstance(x, (dict, list))]
            if str_vals:
                clean_k = re.sub(r"[^a-zA-Z0-9_]", "_", str(k).strip().lower())
                clean_k = re.sub(r"^[^a-zA-Z]+", "", clean_k)
                if clean_k and clean_k not in RESERVED_FIELD_NAMES:
                    custom_fields[clean_k] = ", ".join(str_vals)
        else:
            clean_k = re.sub(r"[^a-zA-Z0-9_]", "_", str(k).strip().lower())
            clean_k = re.sub(r"^[^a-zA-Z]+", "", clean_k)
            if clean_k and clean_k not in RESERVED_FIELD_NAMES:
                custom_fields[clean_k] = str(v).strip()

    # Auto-register missing ContactField definitions
    if custom_fields:
        for k in custom_fields.keys():
            if not ContactField.objects.filter(organization=organization, key=k).exists():
                try:
                    label = k.replace("_", " ").title()
                    ContactField.objects.create(
                        organization=organization,
                        key=k,
                        label=label,
                    )
                except Exception:
                    pass

    source_val = str(
        raw_dict.get("source")
        or raw_dict.get("lead_source")
        or raw_dict.get("vendor")
        or raw_dict.get("source_vendor")
        or raw_dict.get("lead_vendor")
        or raw_dict.get("provider")
        or ""
    ).strip() or source or Contact.Source.API

    data = {
        "phone": raw_phone,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "source": source_val,
        "custom_fields": custom_fields,
    }

    contact, created = save_contact(
        organization=organization,
        data=data,
        source=source,
        region=region,
    )

    # Consent recording
    raw_consent = raw_dict.get("consent") if "consent" in raw_dict else raw_dict.get("opt_in")
    has_consent = default_consent
    if raw_consent is not None:
        if isinstance(raw_consent, bool):
            has_consent = raw_consent
        elif isinstance(raw_consent, (int, float)):
            has_consent = bool(raw_consent)
        elif isinstance(raw_consent, str):
            has_consent = raw_consent.strip().lower() in ("true", "1", "yes", "opt_in", "on")

    if has_consent and contact.sms_status != Contact.SMSStatus.OPTED_OUT:
        from compliance.models import ConsentEvent
        from compliance.services import record_consent
        form_label = custom_fields.get("form") or "API Ingestion"
        record_consent(
            contact=contact,
            event_type=ConsentEvent.EventType.OPT_IN,
            source=ConsentEvent.Source.API,
            proof_text=f"API submission: {form_label}",
        )

    # List assignment
    target_list = default_list
    list_identifier = raw_dict.get("list") or raw_dict.get("list_name") or raw_dict.get("list_id")
    if list_identifier:
        if str(list_identifier).isdigit():
            found_list = ContactList.objects.filter(id=int(list_identifier), organization=organization).first()
            if found_list:
                target_list = found_list
        if not target_list and isinstance(list_identifier, str) and list_identifier.strip():
            list_name = list_identifier.strip()
            target_list, _created_list = ContactList.objects.get_or_create(
                organization=organization,
                name=list_name,
            )

    if target_list:
        target_list.contacts.add(contact)

    return contact, created


def fetch_and_import_remote_leads(
    organization,
    url: str,
    method: str = "GET",
    auth_header: str = "",
    contact_list_id: Optional[int] = None,
    consent_attested: bool = True,
) -> Dict[str, Any]:
    """
    Fetches leads from a remote API URL (JSON) and imports them into the organization.
    """
    import requests
    headers = {"User-Agent": "SparkText-Lead-Ingestion/1.0", "Accept": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header.strip()

    method = method.upper().strip()
    try:
        if method == "POST":
            resp = requests.post(url, headers=headers, json={}, timeout=15)
        else:
            resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ValidationError(f"Failed to fetch data from API URL: {exc}")

    try:
        data = resp.json()
    except Exception:
        raise ValidationError("The remote API URL did not return valid JSON.")

    leads_raw = []
    if isinstance(data, list):
        leads_raw = data
    elif isinstance(data, dict):
        for candidate_key in ["leads", "data", "results", "contacts", "items"]:
            if isinstance(data.get(candidate_key), list):
                leads_raw = data[candidate_key]
                break
        if not leads_raw and any(k in data for k in ["phone", "phone_number", "mobile", "cell"]):
            leads_raw = [data]

    if not leads_raw:
        raise ValidationError("No contacts or leads list found in the API JSON response.")

    target_list = None
    if contact_list_id:
        target_list = ContactList.objects.filter(id=contact_list_id, organization=organization).first()

    created_count = 0
    updated_count = 0
    skipped_count = 0
    errors = []

    for idx, item in enumerate(leads_raw, start=1):
        if not isinstance(item, dict):
            skipped_count += 1
            errors.append({"row": idx, "error": "Item is not a valid JSON object."})
            continue
        try:
            c_obj, created = ingest_lead_data(
                organization=organization,
                raw_dict=item,
                default_list=target_list,
                source=Contact.Source.API,
                default_consent=consent_attested,
            )
            if created:
                created_count += 1
            else:
                updated_count += 1
        except ValidationError as ve:
            skipped_count += 1
            err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
            errors.append({"row": idx, "error": err_msg})
        except Exception as exc:
            skipped_count += 1
            errors.append({"row": idx, "error": str(exc)})

    return {
        "total": len(leads_raw),
        "created": created_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "errors": errors[:20],
        "list_name": target_list.name if target_list else None,
    }
