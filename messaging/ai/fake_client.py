import json
from typing import Any

from .base import AIResponse, BaseAIClient


class FakeAIClient(BaseAIClient):
    """
    Fake AI client for offline testing.
    Never calls any real network endpoint or LLM.
    """

    calls: list[dict[str, Any]] = []
    canned_responses: list[Any] = []
    default_reply: dict[str, Any] = {
        "reply": "Thank you for reaching out! What kind of service do you need?",
        "intent": "inquiry",
        "collected": {},
        "needs_human": False,
        "handoff_reason": None,
    }

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.canned_responses = []
        cls.default_reply = {
            "reply": "Thank you for reaching out! What kind of service do you need?",
            "intent": "inquiry",
            "collected": {},
            "needs_human": False,
            "handoff_reason": None,
        }

    @classmethod
    def push_response(cls, response: Any):
        """Enqueue a string, dict, AIResponse, or Exception to return on next call."""
        cls.canned_responses.append(response)

    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None = None,
    ) -> AIResponse:
        self.calls.append({
            "system_prompt": system_prompt,
            "messages": messages,
            "schema": schema,
        })

        # The focused "which question does this reply answer?" pass only uses a queued response
        # meant for it; otherwise it finds nothing, so tests' scripted replies stay in order.
        is_read_pass = bool(schema and "question_key" in (schema.get("properties") or {}))
        if is_read_pass and not (self.canned_responses and isinstance(self.canned_responses[0], dict)
                                 and "question_key" in self.canned_responses[0]):
            return AIResponse(text=json.dumps({"question_key": "", "answer": ""}), input_tokens=50,
                              output_tokens=10, latency_ms=20)

        # The "are these real answers?" check likewise only uses a response queued for it; otherwise keeps all
        if schema and "verdicts" in (schema.get("properties") or {}) and not (
                self.canned_responses and isinstance(self.canned_responses[0], dict)
                and "verdicts" in self.canned_responses[0]):
            return AIResponse(text=json.dumps({"verdicts": {}}), input_tokens=50, output_tokens=10, latency_ms=20)

        if self.canned_responses:
            next_resp = self.canned_responses.pop(0)
            if isinstance(next_resp, Exception):
                raise next_resp
            if isinstance(next_resp, AIResponse):
                return next_resp
            if isinstance(next_resp, dict):
                return AIResponse(
                    text=json.dumps(next_resp),
                    input_tokens=120,
                    output_tokens=45,
                    latency_ms=85,
                )
            if isinstance(next_resp, str):
                return AIResponse(
                    text=next_resp,
                    input_tokens=100,
                    output_tokens=30,
                    latency_ms=70,
                )

        return AIResponse(
            text=json.dumps(self.default_reply),
            input_tokens=150,
            output_tokens=40,
            latency_ms=65,
        )

