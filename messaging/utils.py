import json
import logging
import urllib.request
from django.conf import settings

logger = logging.getLogger(__name__)


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

