import phonenumbers
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

def get_default_region() -> str:
    return getattr(settings, "DEFAULT_PHONE_REGION", "US")


def normalize_phone(phone_input: str, default_region: str = None) -> str:
    """
    Convert any phone input to E.164 string format using phonenumbers.
    - Default region is read from settings.DEFAULT_PHONE_REGION (defaults to 'US').
    - Rejects phone numbers with extensions (e.g. ext, x, post-dial digits).
    - Rejects invalid phone numbers with a clean ValidationError.
    """
    if not phone_input or not str(phone_input).strip():
        raise ValidationError(_("Phone number cannot be blank."))

    raw_phone = str(phone_input).strip()
    region = default_region or get_default_region()

    try:
        parsed = phonenumbers.parse(raw_phone, region)
    except phonenumbers.NumberParseException as exc:
        raise ValidationError(
            _("Invalid phone number format: %(error)s"),
            params={"error": str(exc)},
            code="invalid_phone",
        )

    # Reject numbers with extensions
    if parsed.extension:
        raise ValidationError(
            _("Phone numbers with extensions are not supported for SMS: '%(ext)s'."),
            params={"ext": parsed.extension},
            code="phone_extension_unsupported",
        )

    if not phonenumbers.is_valid_number(parsed):
        raise ValidationError(
            _("'%(number)s' is not a valid phone number for region '%(region)s'."),
            params={"number": raw_phone, "region": region},
            code="invalid_phone",
        )

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
