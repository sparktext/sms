import logging
from typing import Optional, Dict, Any
from django.conf import settings
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from .base import BaseSMSProvider, ProviderResult

logger = logging.getLogger(__name__)


class TwilioProvider(BaseSMSProvider):
    """
    Twilio SMS implementation using the twilio-python SDK.
    Never logs or exposes auth tokens.
    """

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
    ):
        self.explicit_account_sid = account_sid
        self.explicit_auth_token = auth_token

    def _resolve_credentials(self) -> tuple[str, str]:
        if self.explicit_account_sid and self.explicit_auth_token:
            return self.explicit_account_sid, self.explicit_auth_token

        sid = self.explicit_account_sid or getattr(settings, "TWILIO_ACCOUNT_SID", "")
        token = self.explicit_auth_token or getattr(settings, "TWILIO_AUTH_TOKEN", "")

        # Always re-read .env with overwrite=True so changes take effect immediately without needing server restart
        try:
            from pathlib import Path
            import environ
            env_file = Path(settings.BASE_DIR) / ".env"
            if env_file.exists():
                env = environ.Env()
                environ.Env.read_env(env_file, overwrite=True)
                sid = env("TWILIO_ACCOUNT_SID", default=sid)
                token = env("TWILIO_AUTH_TOKEN", default=token)
        except Exception as exc:
            logger.debug("Failed dynamic .env read: %s", exc)

        return sid, token

    @property
    def account_sid(self) -> str:
        sid, _ = self._resolve_credentials()
        return sid

    @property
    def auth_token(self) -> str:
        _, token = self._resolve_credentials()
        return token

    def _get_client(self) -> Client:
        sid, token = self._resolve_credentials()
        if not sid or not token:
            raise ValueError("Twilio credentials not configured (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN).")
        logger.info("Twilio client initialized with Account SID: %s...", sid[:8] if sid else "None")
        return Client(sid, token)

    def send_sms(
        self,
        from_number: str,
        to_number: str,
        body: str,
        status_callback_url: Optional[str] = None,
    ) -> ProviderResult:
        try:
            client = self._get_client()
            kwargs = {
                "from_": from_number,
                "to": to_number,
                "body": body,
            }
            if status_callback_url:
                kwargs["status_callback"] = status_callback_url

            logger.info(
                "Twilio messages.create called: from=%s, to=%s, status_callback=%s",
                from_number,
                to_number,
                kwargs.get("status_callback"),
            )

            message = client.messages.create(**kwargs)

            logger.info(
                "Twilio message created: SID=%s, initial_status=%s, num_segments=%s",
                message.sid,
                message.status,
                getattr(message, "num_segments", None),
            )

            # Map Twilio status to our internal status choices
            status_map = {
                "queued": "queued",
                "sending": "sending",
                "sent": "sent",
                "delivered": "delivered",
                "undelivered": "undelivered",
                "failed": "failed",
                "receiving": "queued",
                "received": "received",
            }
            normalized_status = status_map.get(str(message.status).lower(), "queued")

            return ProviderResult(
                success=True,
                provider_message_sid=message.sid,
                status=normalized_status,
                num_segments=getattr(message, "num_segments", None),
            )

        except TwilioRestException as exc:
            logger.warning("Twilio API error sending message to %s: %s (code %s)", to_number, exc.msg, exc.code)
            return ProviderResult(
                success=False,
                status="failed",
                error_code=str(exc.code or "TWILIO_ERROR"),
                error_message=str(exc.msg or "Twilio API error"),
            )
        except Exception as exc:
            logger.exception("Unexpected error sending message via Twilio to %s: %s", to_number, exc)
            return ProviderResult(
                success=False,
                status="failed",
                error_code="PROVIDER_ERROR",
                error_message="Unexpected provider failure while sending message.",
            )

    def validate_signature(
        self,
        url: str,
        params: Dict[str, Any],
        signature: str,
    ) -> bool:
        """
        Validates the X-Twilio-Signature header.
        If TWILIO_VALIDATE_WEBHOOKS is False (e.g. local dev / test), returns True.
        """
        validate_enabled = getattr(settings, "TWILIO_VALIDATE_WEBHOOKS", True)
        if not validate_enabled:
            return True

        if not self.auth_token:
            logger.error("Cannot validate Twilio signature: TWILIO_AUTH_TOKEN is missing.")
            return False

        if not signature:
            return False

        try:
            validator = RequestValidator(self.auth_token)
            return validator.validate(url, params, signature)
        except Exception as exc:
            logger.error("Error validating Twilio signature: %s", exc)
            return False

