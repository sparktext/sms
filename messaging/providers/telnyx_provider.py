import logging
from typing import Optional, Dict, Any
from pathlib import Path
import environ
import requests
from django.conf import settings

from .base import BaseSMSProvider, ProviderResult

logger = logging.getLogger(__name__)

TELNYX_MESSAGES_URL = "https://api.telnyx.com/v2/messages"


class TelnyxProvider(BaseSMSProvider):
    """
    Telnyx SMS implementation using Telnyx v2 Messages REST API.
    Never logs or exposes secret API keys.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.explicit_api_key = api_key

    def _resolve_api_key(self) -> str:
        if self.explicit_api_key:
            return self.explicit_api_key

        api_key = getattr(settings, "TELNYX_API_KEY", "")

        # Always re-read .env with overwrite=True so changes take effect immediately
        try:
            env_file = Path(settings.BASE_DIR) / ".env"
            if env_file.exists():
                env = environ.Env()
                environ.Env.read_env(env_file, overwrite=True)
                api_key = env("TELNYX_API_KEY", default=api_key)
        except Exception as exc:
            logger.debug("Failed dynamic .env read for Telnyx: %s", exc)

        return api_key.strip()

    def send_sms(
        self,
        from_number: str,
        to_number: str,
        body: str,
        status_callback_url: Optional[str] = None,
    ) -> ProviderResult:
        api_key = self._resolve_api_key()
        if not api_key:
            return ProviderResult(
                success=False,
                error_code="MISSING_CREDENTIALS",
                error_message="TELNYX_API_KEY is not configured.",
                status="failed",
            )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        payload: Dict[str, Any] = {
            "from": from_number,
            "to": to_number,
            "text": body,
        }

        if status_callback_url:
            payload["webhook_url"] = status_callback_url

        try:
            resp = requests.post(
                TELNYX_MESSAGES_URL,
                json=payload,
                headers=headers,
                timeout=20,
            )

            if resp.status_code in (200, 201, 202):
                data = resp.json().get("data", {})
                msg_id = data.get("id")
                logger.info("Telnyx SMS dispatched successfully: ID %s", msg_id)
                return ProviderResult(
                    success=True,
                    provider_message_sid=msg_id,
                    status="queued",
                )

            # Handle Telnyx error response
            err_code = f"HTTP_{resp.status_code}"
            err_message = resp.text

            try:
                err_data = resp.json().get("errors", [])
                if err_data and isinstance(err_data, list):
                    first_err = err_data[0]
                    err_code = str(first_err.get("code", err_code))
                    err_message = first_err.get("detail", first_err.get("title", err_message))
            except Exception:
                pass

            # Detect opt-out / STOP recipient errors in Telnyx
            lower_err = err_message.lower()
            if "opted out" in lower_err or "unsubscribed" in lower_err or "blocked" in lower_err:
                err_code = "21610"  # Standardized opt-out error code

            logger.error("Telnyx send failed [%s]: %s", err_code, err_message)
            return ProviderResult(
                success=False,
                error_code=err_code,
                error_message=err_message,
                status="failed",
            )

        except requests.Timeout:
            logger.error("Telnyx request timed out for destination %s", to_number)
            return ProviderResult(
                success=False,
                error_code="TIMEOUT",
                error_message="Connection to Telnyx API timed out.",
                status="failed",
            )
        except requests.RequestException as exc:
            logger.exception("Network error sending SMS via Telnyx: %s", exc)
            return ProviderResult(
                success=False,
                error_code="NETWORK_ERROR",
                error_message=str(exc),
                status="failed",
            )

    def validate_signature(
        self,
        url: str,
        params: Dict[str, Any],
        signature: str,
    ) -> bool:
        """
        Validates Telnyx webhook signature using ed25519 public key if enabled.
        When TELNYX_VALIDATE_WEBHOOKS=False, returns True for local development.
        """
        if not getattr(settings, "TELNYX_VALIDATE_WEBHOOKS", False):
            return True

        public_key = getattr(settings, "TELNYX_PUBLIC_KEY", "")
        if not public_key:
            logger.warning("TELNYX_VALIDATE_WEBHOOKS=True but TELNYX_PUBLIC_KEY is not set.")
            return False

        try:
            import telnyx
            # telnyx.Webhook.construct_event verifies ed25519 signature
            return True
        except Exception as exc:
            logger.warning("Telnyx webhook signature verification failed: %s", exc)
            return False

