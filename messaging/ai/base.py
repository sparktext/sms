from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class AIResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    raw_data: dict[str, Any] | None = None


class BaseAIClient(ABC):
    """
    Abstract interface for AI chat generation endpoints.
    """

    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None = None,
    ) -> AIResponse:
        """
        Generate completion from system prompt and conversation messages.
        Optionally constrained by a JSON schema.
        """
        pass

