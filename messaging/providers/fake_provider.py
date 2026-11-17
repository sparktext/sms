import uuid
from typing import Optional, Dict, Any, List
from django.conf import settings

from .base import BaseSMSProvider, ProviderResult


class FakeProvider(BaseSMSProvider):
    """
    Fake SMS provider for unit tests and local mock development.
    Never makes real network requests.
    """
    sent_messages: List[Dict[str, Any]] = []
    fail_with_code: Optional[str] = None
    fail_with_message: str = ""

    @classmethod
    def reset(cls):
        cls.sent_messages.clear()
        cls.fail_with_code = None
        cls.fail_with_message = ""

    def send_sms(
        self,
        from_number: str,
        to_number: str,
        body: str,
        status_callback_url: Optional[str] = None,
    ) -> ProviderResult:
        if self.fail_with_code is not None:
            return ProviderResult(
                success=False,
                status="failed",
                error_code=str(self.fail_with_code),
                error_message=self.fail_with_message or "Simulated provider error",
            )

        sid = f"SM_fake_{uuid.uuid4().hex[:24]}"
        segments = 1 if len(body) <= 160 else (len(body) // 153 + 1)
        record = {
            "sid": sid,
            "from": from_number,
            "to": to_number,
            "body": body,
            "status_callback_url": status_callback_url,
            "num_segments": segments,
        }
        self.sent_messages.append(record)

        return ProviderResult(
            success=True,
            provider_message_sid=sid,
            status="queued",
            num_segments=segments,
        )

    def validate_signature(
        self,
        url: str,
        params: Dict[str, Any],
        signature: str,
    ) -> bool:
        validate_enabled = getattr(settings, "TWILIO_VALIDATE_WEBHOOKS", True)
        if not validate_enabled:
            return True

        # Accept known test signatures
        return signature in ("valid_test_signature", "valid_signature")

