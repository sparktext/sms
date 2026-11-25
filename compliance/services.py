import re
import string
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from contacts.models import Contact

from .models import ConsentEvent

STOP_KEYWORDS = frozenset(["STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"])
START_KEYWORDS = frozenset(["START", "UNSTOP"])
HELP_KEYWORDS = frozenset(["HELP"])

# Opt-outs in the lead's own words. Consent can be revoked "by any reasonable means" (FCC), so these count
# as STOP too. Each needs a texting-related object, so "cancel the call" or "stop by the office" don't.
_STOP_PHRASE_RE = re.compile(
    r"\b(stop|quit|cease)\s+(all\s+)?(the\s+)?(texting|texts?|messaging|messages?|sending|contacting|"
    r"text(ing)?\s+me|messag(e|ing)\s+me|contact(ing)?\s+me|bugging\s+me|spamming)\b"
    r"|\b(don'?t|dont|do\s+not|never)\s+(ever\s+)?(text|message|sms|contact|call\s+or\s+text)\s+me\b"
    r"|\b(remove|take|delete)\s+(me|my\s+(number|info))\s+(off|from)\b"
    r"|\b(remove|delete)\s+my\s+number\b"
    r"|\bunsubscribe\b|\bopt\s*(me\s+)?out\b|\bno\s+more\s+(texts|messages)\b|\blose\s+my\s+number\b"
    r"|\bleave\s+me\s+alone\b|\bnot\s+interested[,.!\s]+stop\b",
    re.I,
)
_NOT_STOP_RE = re.compile(r"\b(don'?t|do\s+not|never)\s+stop\b", re.I)
_POLITE_STOP_WORDS = {"PLEASE", "PLS", "PLZ", "NOW", "THANKS", "THANK", "YOU", "OK", "OKAY", "JUST", "STOP", "STOPALL"}


def is_stop_phrase(text: Optional[str]) -> bool:
    """An opt-out in the lead's own words ("please stop texting me", "remove me from your list"),
    as opposed to the bare STOP keyword that carriers answer automatically."""
    if not text or _NOT_STOP_RE.search(text):
        return False
    if _STOP_PHRASE_RE.search(text):
        return True
    # "stop please", "please stop!", "STOP NOW"
    words = re.findall(r"[A-Za-z]+", text.upper())
    return "STOP" in words and len(words) > 1 and set(words) <= _POLITE_STOP_WORDS


def classify_keyword(text: str | None) -> str | None:
    """
    Classify incoming text into STOP, START, HELP or None.
    - Case-insensitive.
    - Ignores surrounding whitespace and punctuation.
    - Recognizes standard STOP synonyms: STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT.
    - Recognizes START synonyms: START, UNSTOP.
    - HELP is recognized as HELP.
    - 'yes' must NOT be treated as START, on purpose, because the AI may ask yes/no questions.
    - Opt-outs in plain words ("please stop texting me", "remove me from your list") are STOP too.
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

    if is_stop_phrase(text):
        return "STOP"

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

