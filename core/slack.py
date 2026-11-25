"""
Slack alerts: posts chosen events (a hot lead, a call to claim, a hand-off...) to the agency's Slack channel through
an incoming-webhook URL saved in Settings > Slack (owners/admins only).

- Only real Slack webhook addresses are accepted (https://hooks.slack.com/...), so the URL can't be used to make
  this server call anywhere else.
- Posting happens in the background with a short timeout: a slow or broken Slack never delays texts or replies.
- The webhook URL is a secret: pages only ever show its last few characters.
- Under tests nothing is ever sent (tests replace _post).
"""
import logging
import re
import threading

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# (key, what the owner sees in Settings, on by default)
EVENTS = [
    ("hot_lead", "🔥 A lead wants a call right now", True),
    ("hot_lead_waiting", "🔥 Nobody called a hot lead within 10 minutes", True),
    ("team_call", "📅 A new call is waiting to be claimed on the team calendar", True),
    ("needs_human", "⚠️ Gemma handed a chat to a person (not calls: those have their own alerts)", True),
    ("call_booked", "📅 A call was booked with a specific agent", False),
    ("opt_out", "🚫 A lead opted out of texts", False),
]
EVENT_KEYS = [key for key, _, _ in EVENTS]
WEBHOOK_RE = re.compile(r"^https://hooks\.slack\.com/(services|workflows|triggers)/[A-Za-z0-9_\-/]{10,400}$")
TIMEOUT_SECONDS = 5


def valid_webhook(url: str) -> bool:
    return bool(WEBHOOK_RE.match((url or "").strip()))


def masked(url: str) -> str:
    """"https://hooks.slack.com/services/…a1B2" (enough to recognise it, never enough to use it)."""
    url = (url or "").strip()
    if not url:
        return ""
    kind = "workflows" if "/workflows/" in url else ("triggers" if "/triggers/" in url else "services")
    return f"https://hooks.slack.com/{kind}/…{url[-4:]}"


def chosen_events(org) -> set:
    events = getattr(org, "slack_events", None)
    if not isinstance(events, (list, tuple)):
        return {key for key, _, on in EVENTS if on}
    return {e for e in events if e in EVENT_KEYS}


def wants(org, key: str) -> bool:
    return bool(org is not None and getattr(org, "slack_webhook_url", "") and key in chosen_events(org))


def _esc(text) -> str:
    """Slack treats &, < and > as markup."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _absolute(path: str) -> str:
    if not path:
        return ""
    try:
        from messaging.utils import get_public_base_url
        base = (get_public_base_url() or "").rstrip("/")
    except Exception:
        base = ""
    return f"{base}{path}" if base else ""


def build_payload(url: str, title: str, lines=(), link_path: str = "", link_label: str = "Open in SparkText") -> dict:
    """Slack message: a bold title, a few lines and a button to the page in SparkText. Workflow webhooks only take
    plain text, so they get the same content as text."""
    link = _absolute(link_path)
    body = "\n".join(_esc(line) for line in lines if line)
    if "/services/" not in url:
        return {"text": "\n".join(x for x in (title, *[line for line in lines if line], link) if x)}
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{_esc(title)}*" + (f"\n{body}" if body else "")}}]
    if link:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": link_label}, "url": link}]})
    return {"text": title, "blocks": blocks}


def _post(url: str, payload: dict):
    """Send one message. Returns (ok, error). Never sends anything during tests."""
    if getattr(settings, "TESTING", False):
        return False, "Not sent: Slack is never called from tests."
    import requests
    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        return False, f"Couldn't reach Slack ({type(exc).__name__})."
    if resp.status_code == 200:
        return True, ""
    detail = (resp.text or "").strip()[:80]
    return False, f"Slack said {resp.status_code}{': ' + detail if detail else ''}"


def _deliver(org_id: int, url: str, payload: dict):
    from core.models import Organization
    ok, error = _post(url, payload)
    if ok:
        Organization.objects.filter(pk=org_id).update(slack_last_ok_at=timezone.now(), slack_last_error="")
    else:
        Organization.objects.filter(pk=org_id).update(slack_last_error=error[:300])
        logger.warning("Slack post for org %s failed: %s", org_id, error)
    return ok, error


def notify(org, key: str, title: str, lines=(), link_path: str = "", link_label: str = "Open in SparkText") -> None:
    """Post this alert to the agency's Slack channel if they chose it. Never raises, never waits for Slack."""
    try:
        if not wants(org, key):
            return
        url = org.slack_webhook_url.strip()
        payload = build_payload(url, title, lines, link_path, link_label)
        if getattr(settings, "TESTING", False):
            _deliver(org.pk, url, payload)                       # tests run it inline (with _post replaced)
        else:
            threading.Thread(target=_deliver, args=(org.pk, url, payload), daemon=True, name="slack").start()
    except Exception as exc:
        logger.warning("Slack alert %s for org %s failed: %s", key, getattr(org, "pk", None), exc)


def send_test(org):
    """The Settings button: post a test message now and say how it went. Returns (ok, error)."""
    if not getattr(org, "slack_webhook_url", ""):
        return False, "Add a Slack webhook URL first."
    payload = build_payload(org.slack_webhook_url, f"✅ SparkText is connected to this channel ({org.name})",
                            ["You'll get the alerts chosen in Settings → Slack here."], "/settings/slack/",
                            "Slack settings")
    return _deliver(org.pk, org.slack_webhook_url.strip(), payload)


def lead_name(contact) -> str:
    if contact is None:
        return "A lead"
    name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
    return name or contact.phone
