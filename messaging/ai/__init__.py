from django.conf import settings
from .base import BaseAIClient, AIResponse
from .openai_client import OpenAICompatibleClient
from .fake_client import FakeAIClient


def get_ai_client() -> BaseAIClient:
    """
    Factory to return the configured AI client.
    Uses FakeAIClient when AI_CLIENT="fake" (for testing).
    """
    client_name = getattr(settings, "AI_CLIENT", "openai").lower()
    if client_name == "fake":
        return FakeAIClient()
    return OpenAICompatibleClient()


__all__ = [
    "BaseAIClient",
    "AIResponse",
    "OpenAICompatibleClient",
    "FakeAIClient",
    "get_ai_client",
]

