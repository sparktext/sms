import time
import json
import logging
from typing import Optional, Dict, Any, List
import requests
from django.conf import settings

from .base import BaseAIClient, AIResponse

logger = logging.getLogger(__name__)


class OpenAICompatibleClient(BaseAIClient):
    """
    Client for OpenAI-compatible local/remote chat endpoints (e.g. LM Studio, Ollama, vLLM).
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ):
        self.base_url = (base_url or getattr(settings, "AI_BASE_URL", "http://localhost:1235/v1")).rstrip("/")
        self.model = model or getattr(settings, "AI_MODEL", "google/gemma-4-e4b")
        self.api_key = api_key or getattr(settings, "AI_API_KEY", "")
        self.timeout = timeout or getattr(settings, "AI_TIMEOUT_SECONDS", 45)
        self.max_retries = max_retries if max_retries is not None else getattr(settings, "AI_MAX_RETRIES", 1)

    def generate(
        self,
        system_prompt: str,
        messages: List[Dict[str, str]],
        schema: Optional[Dict[str, Any]] = None,
    ) -> AIResponse:
        endpoint = f"{self.base_url}/chat/completions"

        chat_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            chat_messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": chat_messages,
            "temperature": 0.2,
        }

        # Request structured JSON format if schema provided
        if schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "ai_assistant_reply",
                    "schema": schema,
                    "strict": True,
                },
            }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            t_start = time.perf_counter()
            try:
                logger.info(
                    "Sending AI request to %s (model: %s, attempt %d/%d)",
                    endpoint,
                    self.model,
                    attempt + 1,
                    self.max_retries + 1,
                )
                response = requests.post(endpoint, json=payload, headers=headers, timeout=self.timeout)
                response.raise_for_status()

                latency_ms = int((time.perf_counter() - t_start) * 1000)
                data = response.json()

                content = ""
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")

                usage = data.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)

                return AIResponse(
                    text=content,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    raw_data=data,
                )

            except Exception as exc:
                last_exc = exc
                latency_ms = int((time.perf_counter() - t_start) * 1000)
                logger.warning(
                    "AI generation failed on attempt %d: %s (latency: %dms)",
                    attempt + 1,
                    exc,
                    latency_ms,
                )
                if attempt < self.max_retries:
                    time.sleep(1.0)

        raise RuntimeError(f"AI generation failed after {self.max_retries + 1} attempt(s): {last_exc}") from last_exc

