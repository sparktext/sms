from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class ProviderResult:
    success: bool
    provider_message_sid: Optional[str] = None
    status: str = "queued"
    num_segments: Optional[int] = None
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
        status_callback_url: Optional[str] = None,
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
        params: Dict[str, Any],
        signature: str,
    ) -> bool:
        """
        Validate incoming webhook signature.
        """
        pass

