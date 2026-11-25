from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from messaging.ai.openai_client import OpenAICompatibleClient


def _ok_response():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "choices": [{"message": {"content": '{"reply": "hi"}'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    return resp


class DisableThinkingTests(TestCase):
    """Production llama.cpp server: reasoning tokens must be switched off per request."""

    def _sent_payload(self):
        client = OpenAICompatibleClient(base_url="http://ai:8080/v1", model="m", api_key="k")
        with patch("messaging.ai.openai_client.requests.post", return_value=_ok_response()) as post:
            client.generate("system", [{"role": "user", "content": "hi"}])
        return post.call_args.kwargs["json"]

    @override_settings(AI_DISABLE_THINKING=True)
    def test_thinking_disabled_when_setting_on(self):
        self.assertEqual(self._sent_payload()["chat_template_kwargs"], {"enable_thinking": False})

    @override_settings(AI_DISABLE_THINKING=False)
    def test_payload_unchanged_when_setting_off(self):
        # Keeps plain OpenAI-compatible APIs working: no unknown fields unless asked for.
        self.assertNotIn("chat_template_kwargs", self._sent_payload())


@override_settings(AI_BASE_URL="http://ai:8080/v1", AI_API_KEY="secret-key")
class AiPingAuthTests(TestCase):
    """The inbox 'AI online?' check must authenticate, or a key-protected server looks offline."""

    def setUp(self):
        # No password needed: force_login skips authentication (and no secret-looking strings in git)
        self.user = get_user_model().objects.create_user(email="pinger@example.com", password=None)
        self.client.force_login(self.user)

    def test_ping_sends_api_key_and_reports_online(self):
        with patch("requests.get", return_value=MagicMock(status_code=200)) as get:
            resp = self.client.get("/ai-assistant/ping/")
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer secret-key"})
        self.assertEqual(resp.json()["status"], "online")

    @override_settings(AI_API_KEY="")
    def test_ping_without_key_sends_no_auth_header(self):
        with patch("requests.get", return_value=MagicMock(status_code=200)) as get:
            self.client.get("/ai-assistant/ping/")
        self.assertEqual(get.call_args.kwargs["headers"], {})
