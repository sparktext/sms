import re
import string
from typing import Optional
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import ConsentEvent
from contacts.models import Contact

STOP_KEYWORDS = frozenset(["STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"])
START_KEYWORDS = frozenset(["START", "UNSTOP"])
HELP_KEYWORDS = frozenset(["HELP"])


def classify_keyword(text: Optional[str]) -> Optional[str]:
    """
    Classify incoming text into STOP, START, HELP or None.
    - Case-insensitive.
    - Ignores surrounding whitespace and punctuation.
    - Recognizes standard STOP synonyms: STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT.
    - Recognizes START synonyms: START, UNSTOP.
    - HELP is recognized as HELP.
    - 'yes' must NOT be treated as START, on purpose, because the AI may ask yes/no questions.
    """
    if not text:
        return None

    # Strip surrounding punctuation and whitespace
    cleaned = text.strip()
    # Strip leading/trailing punctuation characters (e.g. "STOP!", ".start.", "#help?")
    cleaned = cleaned.strip(string.punctuation + string.whitespace).upper()

    # Safety check: if empty after stripping
    if not cleaned:
        return None

    # Explicit check: 'YES' must NEVER be treated as START
    if cleaned == "YES":
        return None

    if cleaned in STOP_KEYWORDS:
        return "STOP"

    if cleaned in START_KEYWORDS:
        return "START"

    if cleaned in HELP_KEYWORDS:
        return "HELP"

    return None


def record_consent(
    contact: Contact,
    event_type: str,
    source: str,
    proof_text: str,
) -> ConsentEvent:
    """
    Writes the ConsentEvent row AND updates contact.sms_status in one atomic transaction.
    """
    if event_type not in ConsentEvent.EventType.values:
        raise ValidationError(_("Invalid event_type: '%(type)s'"), params={"type": event_type})

    if source not in ConsentEvent.Source.values:
        raise ValidationError(_("Invalid source: '%(src)s'"), params={"src": source})

    if not proof_text or not str(proof_text).strip():
        raise ValidationError(_("proof_text cannot be blank."))

    with transaction.atomic():
        # Determine target sms_status
        if event_type == ConsentEvent.EventType.OPT_IN:
            target_status = Contact.SMSStatus.OPTED_IN
        elif event_type == ConsentEvent.EventType.OPT_OUT:
            target_status = Contact.SMSStatus.OPTED_OUT
        else:
            raise ValidationError(_("Unhandled event type."))

        # Create append-only ledger entry
        event = ConsentEvent.objects.create(
            contact=contact,
            organization=contact.organization,
            event_type=event_type,
            source=source,
            proof_text=proof_text.strip(),
        )

        # Update contact status atomically
        contact.sms_status = target_status
        contact.save(update_fields=["sms_status", "updated_at"])

    return event


def can_send_marketing(contact: Contact) -> bool:
    """
    True ONLY if sms_status == opted_in.
    """
    if not contact:
        return False
    return contact.sms_status == Contact.SMSStatus.OPTED_IN


def can_reply(contact: Contact) -> bool:
    """
    True unless sms_status == opted_out.
    This covers customers who text first (unknown status can receive replies).
    """
    if not contact:
        return False
    return contact.sms_status != Contact.SMSStatus.OPTED_OUT

