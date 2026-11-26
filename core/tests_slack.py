"""Slack alerts: the settings page (owners/admins), the webhook URL rules, and each alert reaching Slack."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings

from contacts.models import Contact
from core import slack
from core.models import Organization, User
from scheduling.models import CalendarEvent
from scheduling.reminders import send_due_reminders
from scheduling.tests_team_calendar import TeamCallTestCase

HOOK = "https://hooks.slack.com/services/T000AAAA/B000BBBB/abcdefghijklmnopqrstuvwx"


class WebhookRulesTests(TestCase):
    def test_only_real_slack_webhooks(self):
        for url in (HOOK, "https://hooks.slack.com/workflows/T01/A02/123/abcdefABCDEF",
                    "https://hooks.slack.com/triggers/T01/123/abcdefABCDEF"):
            self.assertTrue(slack.valid_webhook(url), url)
        for url in ("http://hooks.slack.com/services/T0/B0/x", "https://hooks.slack.com.evil.com/services/T0/B0/xxxxxxxx",
                    "https://example.com/services/T0/B0/xxxxxxxxxx", "javascript:alert(1)", "",
                    "https://hooks.slack.com/services/T0/B0/x?y=1", "https://user@hooks.slack.com/services/T0/B0/xxxxxxxx"):
            self.assertFalse(slack.valid_webhook(url), url)

    def test_the_url_is_never_shown_in_full(self):
        self.assertEqual(slack.masked(HOOK), "https://hooks.slack.com/services/…uvwx")
        self.assertEqual(slack.masked(""), "")

    def test_lead_names_cant_break_slack_formatting(self):
        payload = slack.build_payload(HOOK, "🔥 <b>Bob</b> & co", ["<!channel> hi"])
        self.assertIn("&lt;!channel&gt; hi", payload["blocks"][0]["text"]["text"])
        self.assertIn("&lt;b&gt;Bob&lt;/b&gt; &amp; co", payload["blocks"][0]["text"]["text"])

    @patch("requests.post")
    def test_nothing_is_ever_sent_from_tests(self, real_post):
        self.assertEqual(slack._post(HOOK, {"text": "x"})[0], False)
        real_post.assert_not_called()


class SlackSettingsPageTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Slack Co", slug="slack-co")
        self.owner = User.objects.create_user(email="o@slack.test", password="pw-123456789", organization=self.org,
                                              role=User.Role.OWNER)
        self.agent = User.objects.create_user(email="a@slack.test", password="pw-123456789", organization=self.org,
                                              role=User.Role.AGENT)

    def post(self, user, **data):
        self.client.force_login(user)
        return self.client.post("/settings/slack/", data)

    def test_owner_connects_chooses_alerts_and_the_url_stays_secret(self):
        self.post(self.owner, action="save", slack_webhook_url=HOOK, slack_events=["hot_lead", "opt_out"])
        self.org.refresh_from_db()
        self.assertEqual((self.org.slack_webhook_url, sorted(self.org.slack_events)), (HOOK, ["hot_lead", "opt_out"]))
        html = self.client.get("/settings/slack/").content.decode()
        self.assertNotIn(HOOK, html)
        self.assertNotIn("abcdefghijklmnop", html)
        self.assertIn("…uvwx", html)
        # saving again with the box empty keeps the URL
        self.post(self.owner, action="save", slack_webhook_url="", slack_events=["hot_lead"])
        self.org.refresh_from_db()
        self.assertEqual((self.org.slack_webhook_url, self.org.slack_events), (HOOK, ["hot_lead"]))

    def test_a_wrong_url_is_refused(self):
        self.post(self.owner, action="save", slack_webhook_url="https://example.com/hook/123456789012")
        self.org.refresh_from_db()
        self.assertEqual(self.org.slack_webhook_url, "")

    def test_agents_can_look_but_not_change(self):
        self.post(self.agent, action="save", slack_webhook_url=HOOK)
        self.org.refresh_from_db()
        self.assertEqual(self.org.slack_webhook_url, "")
        self.assertIn("Viewing mode", self.client.get("/settings/slack/").content.decode())

    def test_test_message_and_disconnect(self):
        self.post(self.owner, action="save", slack_webhook_url=HOOK, slack_events=["hot_lead"])
        with patch("core.slack._post", return_value=(True, "")) as sent:
            self.post(self.owner, action="test")
        url, payload = sent.call_args[0]
        self.assertEqual(url, HOOK)
        self.assertIn("SparkText is connected", payload["text"])
        self.org.refresh_from_db()
        self.assertIsNotNone(self.org.slack_last_ok_at)
        with patch("core.slack._post", return_value=(False, "Slack said 404: no_service")):
            self.post(self.owner, action="test")
        self.org.refresh_from_db()
        self.assertEqual(self.org.slack_last_error, "Slack said 404: no_service")
        self.assertIn("no_service", self.client.get("/settings/slack/").content.decode())
        self.post(self.owner, action="disconnect")
        self.org.refresh_from_db()
        self.assertEqual(self.org.slack_webhook_url, "")

    def test_every_settings_page_links_to_slack(self):
        self.client.force_login(self.owner)
        for url in ("/settings/profile/", "/settings/team/", "/settings/sounds/"):
            self.assertIn("/settings/slack/", self.client.get(url).content.decode(), url)


@patch("scheduling.calls.can_call_now", return_value=(True, ""))
class SlackAlertsTests(TeamCallTestCase):
    def setUp(self):
        super().setUp()
        self.org.slack_webhook_url = HOOK
        self.org.slack_events = [k for k in slack.EVENT_KEYS]
        self.org.save()
        patcher = patch("core.slack._post", return_value=(True, ""))
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def titles(self):
        return [c.args[1]["text"] for c in self.post.call_args_list]

    def test_a_hot_lead_posts_with_a_link_to_the_chat(self, _):
        conv = self.conv("CA")
        with patch("messaging.utils.get_public_base_url", return_value="https://app.example.com"):
            self.say(conv, "can you call me now?", "Sure!")
        [hot] = [c.args[1] for c in self.post.call_args_list if "wants a call right now" in c.args[1]["text"]]
        self.assertEqual(hot["text"], "🔥 Maria Lopez wants a call right now")
        self.assertIn(conv.contact.phone, hot["blocks"][0]["text"]["text"])
        self.assertEqual(hot["blocks"][1]["elements"][0]["url"], f"https://app.example.com/inbox/{conv.id}/")
        self.assertFalse(any("needs a person" in t for t in self.titles()))       # no duplicate hand-off post

    def test_a_hot_lead_nobody_called_posts_again_after_10_minutes(self, _):
        conv = self.conv("CA")
        self.say(conv, "call me now please", "Sure!")
        event = CalendarEvent.objects.get(conversation=conv)
        send_due_reminders(now=event.start_at + timedelta(minutes=10))
        self.assertTrue(any(t == "🔥 Still waiting: nobody has reached Maria Lopez 10 minutes after they asked"
                            for t in self.titles()), self.titles())

    def test_a_call_to_claim_posts_and_a_booked_call_too_when_chosen(self, _):
        self.book_tomorrow_10(self.conv("CA"))
        self.assertTrue(any(t == "📅 New call to claim: Maria Lopez" for t in self.titles()))
        self.post.reset_mock()
        self.book_tomorrow_10(self.conv("CA", assigned=self.agent2))
        self.assertTrue(any(t == "📅 Call booked: Maria Lopez with Bea" for t in self.titles()))

    def test_a_hand_off_posts(self, _):
        conv = self.conv("CA")
        self.say(conv, "I have a question about my claim", "Let me get someone.", reason="Claim question")
        [title] = [t for t in self.titles() if "needs a person" in t]
        self.assertEqual(title, "⚠️ Maria Lopez needs a person")

    def test_an_opt_out_posts(self, _):
        conv = self.conv("CA")
        with self.settings(TWILIO_VALIDATE_WEBHOOKS=True):
            self.client.post("/webhooks/twilio/sms/", {"From": conv.contact.phone, "To": self.phone.e164,
                                                       "Body": "STOP", "MessageSid": "SM_slack_stop"},
                             HTTP_X_TWILIO_SIGNATURE="valid_test_signature")
        self.assertTrue(any(t == "🚫 Maria Lopez opted out of texts" for t in self.titles()))

    def test_alerts_that_are_switched_off_or_not_connected_post_nothing(self, _):
        self.org.slack_events = ["opt_out"]
        self.org.save()
        self.say(self.conv("CA"), "can you call me now?", "Sure!")
        self.book_tomorrow_10(self.conv("CA"))
        self.assertEqual(self.titles(), [])
        self.org.slack_webhook_url = ""
        self.org.slack_events = list(slack.EVENT_KEYS)
        self.org.save()
        self.book_tomorrow_10(self.conv("CA"))
        self.assertEqual(self.titles(), [])

    def test_a_slack_failure_never_breaks_booking(self, _):
        self.post.side_effect = RuntimeError("Slack is down")
        event = self.book_tomorrow_10(self.conv("CA"))
        self.assertIsNotNone(event.pk)


class OtherWorkspacesTests(TestCase):
    @override_settings(MESSAGING_PROVIDER="fake")
    def test_a_workspace_without_slack_never_posts(self):
        org = Organization.objects.create(name="Plain", slug="plain")
        Contact.objects.create(organization=org, phone="+16505557777")
        with patch("core.slack._post") as sent:
            slack.notify(org, "hot_lead", "x")
        sent.assert_not_called()
