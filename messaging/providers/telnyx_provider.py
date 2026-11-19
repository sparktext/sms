import base64
import binascii
import logging
import time
from typing import Optional, Dict, Any, Union
from pathlib import Path
import environ
import requests
from django.conf import settings

from .base import BaseSMSProvider, ProviderResult

logger = logging.getLogger(__name__)

TELNYX_MESSAGES_URL = "https://api.telnyx.com/v2/messages"

# Reject webhooks whose signed timestamp is further than this from now (replay protection)
TELNYX_WEBHOOK_TOLERANCE_SECONDS = 300


def _load_telnyx_public_key(raw_key: str):
    """
    Load the Telnyx account public key (Mission Control -> Keys & Credentials -> Public Key).
    Accepts the base64 form Telnyx shows (padded or unpadded, standard or URL-safe) or PEM.
    Returns an Ed25519PublicKey, or None when the key is unusable.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    key = (raw_key or "").strip()
    if not key:
        return None
    try:
        if key.startswith("-----BEGIN"):
            loaded = load_pem_public_key(key.encode())
            return loaded if isinstance(loaded, Ed25519PublicKey) else None
        padded = key + "=" * (-len(key) % 4)
        decoder = base64.urlsafe_b64decode if ("-" in key or "_" in key) else base64.b64decode
        raw = decoder(padded)
        if len(raw) != 32:
            return None
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError, binascii.Error):
        return None


def verify_telnyx_webhook(
    raw_body: Union[bytes, str],
    signature_b64: str,
    timestamp: str,
    public_key: Optional[str] = None,
    now: Optional[float] = None,
    tolerance: int = TELNYX_WEBHOOK_TOLERANCE_SECONDS,
) -> bool:
    """
    Verify a Telnyx API v2 webhook. Fails closed: any missing or malformed input returns False.

    Telnyx signs  f"{telnyx-timestamp}|{raw request body}"  with Ed25519 and sends the
    base64 signature in the `telnyx-signature-ed25519` header.
    """
    from cryptography.exceptions import InvalidSignature

    if public_key is None:
        public_key = getattr(settings, "TELNYX_PUBLIC_KEY", "")
    key = _load_telnyx_public_key(public_key)
    if key is None:
        logger.error("Telnyx webhook rejected: TELNYX_PUBLIC_KEY is missing or invalid.")
        return False

    if not signature_b64 or not timestamp:
        return False
    try:
        ts = int(str(timestamp).strip())
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance:
        logger.warning("Telnyx webhook rejected: timestamp outside %ss tolerance.", tolerance)
        return False

    try:
        signature = base64.b64decode(signature_b64.strip(), validate=True)
    except (ValueError, binascii.Error):
        return False

    body = raw_body if isinstance(raw_body, bytes) else str(raw_body).encode("utf-8")
    try:
        key.verify(signature, f"{ts}|".encode("utf-8") + body)
        return True
    except InvalidSignature:
        return False


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
        Telnyx signatures cover the raw request body plus a timestamp header, which this
        URL/params interface cannot provide. Always fails closed; webhook views must use
        verify_telnyx_webhook(raw_body, signature, timestamp) instead.
        """
        logger.error(
            "TelnyxProvider.validate_signature() cannot verify Telnyx webhooks; "
            "use verify_telnyx_webhook() with the raw body and headers."
        )
        return False

