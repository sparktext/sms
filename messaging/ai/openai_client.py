import logging
import time
from typing import Any

import requests
from django.conf import settings

from .base import AIResponse, BaseAIClient

logger = logging.getLogger(__name__)


class OpenAICompatibleClient(BaseAIClient):
    """
    Client for OpenAI-compatible local/remote chat endpoints (e.g. LM Studio, Ollama, vLLM).
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
        max_retries: int | None = None,
    ):
        self.base_url = (base_url or getattr(settings, "AI_BASE_URL", "http://localhost:1235/v1")).rstrip("/")
        if not model:
            try:
                from pathlib import Path

                import environ
                env_file = Path(settings.BASE_DIR) / ".env"
                if env_file.exists():
                    env = environ.Env()
                    environ.Env.read_env(env_file, overwrite=True)
                    model = env("AI_MODEL", default=getattr(settings, "AI_MODEL", "google/gemma-4-12b-qat"))
            except Exception:
                pass
        self.model = model or getattr(settings, "AI_MODEL", "google/gemma-4-12b-qat")
        self.api_key = api_key or getattr(settings, "AI_API_KEY", "")
        self.timeout = timeout or getattr(settings, "AI_TIMEOUT_SECONDS", 45)
        self.max_retries = max_retries if max_retries is not None else getattr(settings, "AI_MAX_RETRIES", 1)

    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None = None,
    ) -> AIResponse:
        endpoint = f"{self.base_url}/chat/completions"

        chat_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            chat_messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": chat_messages,
            "temperature": 0.4,
        }
        if getattr(settings, "AI_DISABLE_THINKING", False):
            # llama.cpp / LM Studio: render the chat template with thinking switched off
            payload["chat_template_kwargs"] = {"enable_thinking": False}

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

        last_exc: Exception | None = None
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
                if response.status_code >= 400:
                    # If local server rejected strict json_schema, fallback to standard json_object on next attempt
                    if response.status_code == 400 and payload.get("response_format", {}).get("type") == "json_schema":
                        payload["response_format"] = {"type": "json_object"}
                    err_detail = (response.text or "").strip()[:140] or response.reason
                    raise RuntimeError(f"HTTP {response.status_code}: {err_detail}")

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
