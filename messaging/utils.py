import json
import logging
import urllib.request
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def format_chat_timestamp(dt=None) -> str:
    """Format a message timestamp consistently with date and time (e.g. 'Nov 06, 05:18 PM')."""
    if not dt:
        dt = timezone.now()
    return timezone.localtime(dt).strftime("%b %d, %I:%M %p")


def get_org_status_counts(org_or_id) -> dict:
    """Return conversation counts keyed by status (open, needs_human, closed) for an organization."""
    from django.db.models import Count
    from .models import Conversation

    org_id = getattr(org_or_id, "id", org_or_id)
    if not org_id:
        return {"open": 0, "needs_human": 0, "closed": 0}
    status_counts_map = dict(
        Conversation.objects.filter(organization_id=org_id)
        .values("status")
        .annotate(c=Count("id"))
        .values_list("status", "c")
    )
    return {
        "open": status_counts_map.get(Conversation.Status.OPEN, 0),
        "needs_human": status_counts_map.get(Conversation.Status.NEEDS_HUMAN, 0),
        "closed": status_counts_map.get(Conversation.Status.CLOSED, 0),
    }


def get_public_base_url() -> str:
    """
    Returns the configured PUBLIC_BASE_URL (e.g. from .env).
    In local development (DEBUG=True), if PUBLIC_BASE_URL is unset,
    auto-discovers an active local ngrok tunnel via the ngrok 127.0.0.1:4040 API.
    """
    base = getattr(settings, "PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return base

    # Automatic local development fallback for ngrok
    if getattr(settings, "DEBUG", False):
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:4040/api/tunnels",
                headers={"User-Agent": "SparkText-Dev"},
            )
            with urllib.request.urlopen(req, timeout=0.25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for tunnel in data.get("tunnels", []):
                    if tunnel.get("proto") == "https" and tunnel.get("public_url"):
                        discovered = tunnel["public_url"].rstrip("/")
                        logger.info("Auto-discovered active ngrok tunnel for webhook callbacks: %s", discovered)
                        return discovered
        except Exception:
            pass

    return ""

