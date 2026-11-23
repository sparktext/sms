from django.conf import settings

from .base import BaseSMSProvider, ProviderResult
from .fake_provider import FakeProvider
from .telnyx_provider import TelnyxProvider
from .twilio_provider import TwilioProvider


def get_sms_provider() -> BaseSMSProvider:
    """
    Factory to return configured SMS provider.
    MESSAGING_PROVIDER choices: 'telnyx', 'twilio', 'fake'.
    Dynamically checks .env so long-running workers pick up changes immediately.
    """
    provider_name = getattr(settings, "MESSAGING_PROVIDER", "").lower()
    if provider_name == "fake":
        return FakeProvider()

    # Single source of truth for "are we under test?" (works for manage.py test AND pytest)
    is_testing = getattr(settings, "TESTING", False)
    if not is_testing:
        try:
            from pathlib import Path

            import environ
            env_file = Path(settings.BASE_DIR) / ".env"
            if env_file.exists():
                env = environ.Env()
                environ.Env.read_env(env_file, overwrite=True)
                provider_name = env("MESSAGING_PROVIDER", default=provider_name or "twilio").lower()
        except Exception:
            pass

    if provider_name == "fake":
        return FakeProvider()
    elif provider_name == "telnyx":
        return TelnyxProvider()
    return TwilioProvider()


__all__ = [
    "BaseSMSProvider",
    "ProviderResult",
    "TwilioProvider",
    "TelnyxProvider",
    "FakeProvider",
    "get_sms_provider",
]

