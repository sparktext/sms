from django.conf import settings
from .base import BaseSMSProvider, ProviderResult
from .twilio_provider import TwilioProvider
from .fake_provider import FakeProvider


def get_sms_provider() -> BaseSMSProvider:
    """
    Factory to return configured SMS provider.
    Set MESSAGING_PROVIDER="fake" in settings/tests for offline mocking.
    """
    provider_name = getattr(settings, "MESSAGING_PROVIDER", "twilio").lower()
    if provider_name == "fake":
        return FakeProvider()
    return TwilioProvider()


__all__ = [
    "BaseSMSProvider",
    "ProviderResult",
    "TwilioProvider",
    "FakeProvider",
    "get_sms_provider",
]

