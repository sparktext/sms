from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ProviderResult:
    success: bool
    provider_message_sid: str | None = None
    status: str = "queued"
    num_segments: int | None = None
    error_code: str = ""
    error_message: str = ""


class BaseSMSProvider(ABC):
    """
    Abstract interface for SMS providers (Twilio, Fake for testing).
    """

    @abstractmethod
    def send_sms(
        self,
        from_number: str,
        to_number: str,
        body: str,
        status_callback_url: str | None = None,
    ) -> ProviderResult:
        """
        Dispatch an SMS via the provider.
        Must NOT raise raw provider exceptions; always return ProviderResult.
        """
        pass

    @abstractmethod
    def validate_signature(
        self,
        url: str,
        params: dict[str, Any],
        signature: str,
    ) -> bool:
        """
        Validate incoming webhook signature.
        """
        pass

