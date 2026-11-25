""""Call me now" (hot leads): ring the agents until someone clicks Call, Ignore, the 3-minute hand-off, the 10-minute
owner alert, and what the lead is told."""
from datetime import datetime, timedelta
from datetime import time as time_cls
from unittest.mock import patch

from django.utils import timezone

from core.models import Organization, User
from messaging.ai.pipeline import _wants_call_now
from messaging.models import Conversation, Message
from scheduling import calls
from scheduling.models import CalendarEvent, ClosedDay
from scheduling.reminders import send_due_reminders
from scheduling.tests_calls import LA
from scheduling.tests_team_calendar import TeamCallTestCase


class SpottingCallMeNowTests(TeamCallTestCase):
    def test_what_counts(self):
        for text in ("call me now", "can you call me right now?", "now", "Now!", "asap", "im free now",
                     "i am free right now", "now please", "as soon as possible", "now is good"):
            self.assertTrue(_wants_call_now(text), text)
        for text in ("not now", "i cant talk now", "busy now, call later", "no", "i need insurance now",
                     "call me tomorrow", "dont call me now"):
            self.assertFalse(_wants_call_now(text), text)

    def test_when_an_agent_can_call_now(self):
        tz = "America/Los_Angeles"
        self.org.call_days, self.org.call_start, self.org.call_end = [0, 1, 2, 3, 4], time_cls(9), time_cls(18)
        self.org.save()
        wednesday_noon = datetime(2027, 1, 6, 12, 0, tzinfo=LA)
        self.assertEqual(calls.can_call_now(self.org, tz, wednesday_noon), (True, ""))
        self.assertEqual(calls.can_call_now(self.org, tz, datetime(2027, 1, 6, 19, 0, tzinfo=LA))[1],
                         "our agents aren't available to call right now")                 # after hours
        ClosedDay.objects.create(organization=self.org, date=wednesday_noon.date(), name="Office party")
        org = Organization.objects.get(pk=self.org.pk)
        self.assertEqual(calls.can_call_now(org, tz, wednesday_noon)[1], "our office is closed today (Office party)")
        ClosedDay.objects.all().delete()
        org = Organization.objects.get(pk=self.org.pk)
        User.objects.filter(organization=self.org).update(is_available_for_leads=False)    # everyone Away
        self.assertFalse(calls.can_call_now(org, tz, wednesday_noon)[0])


@patch("scheduling.calls.can_call_now", return_value=(True, ""))
class HotLeadTests(TeamCallTestCase):
    def now_call(self, conv, text="can you call me now?"):
        return self.say(conv, text, "Sure, I'll have someone call you!")

    def hot(self, user):
        self.client.force_login(user)
        return self.client.get("/calls/hot/").json()["leads"]

    def test_a_new_lead_rings_every_available_agent_and_is_told_a_few_minutes(self, _):
        conv = self.conv("CA")
        with patch("scheduling.calls.refresh_urgent") as refresh:
            reply = self.now_call(conv)
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertTrue(event.urgent)
        self.assertIsNone(event.user)                                              # the team: first to Call gets it
        self.assertLess(event.start_at - timezone.now(), timedelta(minutes=2))      # now, not the next quarter hour
        self.assertIsNotNone(event.start_alert_sent_at)                            # the ringing replaces reminders
        self.assertIn("few minutes", reply)
        self.assertNotIn("?", reply)
        conv.refresh_from_db()
        self.assertEqual((conv.status, conv.handoff_reason, conv.assigned_to),
                         (Conversation.Status.NEEDS_HUMAN, "🔥 Wants a call right now", None))
        self.assertIn("🔥 Hot lead: they want a call right now. Ringing the team.", self.call_notes_any(conv))
        self.assertTrue(refresh.called)
        [lead] = self.hot(self.agent)
        self.assertEqual((lead["ring"], lead["can_call"], lead["phone"]), (True, True, conv.contact.phone))
        [lead] = self.hot(self.owner)                                              # not available: banner, no ring
        self.assertEqual((lead["ring"], lead["can_call"]), (False, True))

    def test_call_takes_it_and_stops_the_ringing_for_everyone(self, _):
        conv = self.conv("CA")
        self.now_call(conv)
        event = CalendarEvent.objects.get(conversation=conv)
        self.client.force_login(self.agent2)
        r = self.client.post(f"/calls/hot/{event.id}/answer/")
        self.assertEqual(r.json(), {"status": "success", "phone": conv.contact.phone, "chat_url": f"/inbox/{conv.id}/"})
        event.refresh_from_db()
        conv.refresh_from_db()
        self.assertEqual((event.user, conv.assigned_to), (self.agent2, self.agent2))
        self.assertIsNotNone(event.answered_at)
        self.assertIn("Bea is calling the lead now", " ".join(Message.objects.filter(conversation=conv)
                                                              .values_list("body", flat=True)))
        self.assertEqual(self.hot(self.agent), [])                                 # banner and ringing gone
        self.client.force_login(self.agent)
        r = self.client.post(f"/calls/hot/{event.id}/answer/")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["message"], "Bea is already calling this lead.")

    def test_ignore_stops_my_ringing_and_when_everyone_ignores_the_owner_gets_it(self, _):
        conv = self.conv("CA")
        self.now_call(conv)
        event = CalendarEvent.objects.get(conversation=conv)
        self.client.force_login(self.agent)
        self.client.post(f"/calls/hot/{event.id}/ignore/")
        self.assertFalse(self.hot(self.agent)[0]["ring"])                          # still on the banner, silent
        self.assertTrue(self.hot(self.agent2)[0]["ring"])
        self.client.force_login(self.agent2)
        self.client.post(f"/calls/hot/{event.id}/ignore/")
        event.refresh_from_db()
        self.assertEqual(event.user, self.owner)                                   # nobody took it
        self.assertTrue(self.hot(self.owner)[0]["ring"])
        self.assertFalse(self.hot(self.agent)[0]["can_call"])                      # it's the owner's now
        self.assertIn("Everyone who was asked ignored this hot lead", " ".join(
            Message.objects.filter(conversation=conv).values_list("body", flat=True)))

    def test_nobody_reacts_for_3_minutes_so_it_is_assigned_and_keeps_ringing_for_them(self, _):
        conv = self.conv("CA")
        self.now_call(conv)
        event = CalendarEvent.objects.get(conversation=conv)
        send_due_reminders(now=event.pooled_at + timedelta(minutes=2, seconds=59))
        event.refresh_from_db()
        self.assertIsNone(event.user)
        send_due_reminders(now=event.pooled_at + timedelta(minutes=3))
        event.refresh_from_db()
        self.assertIn(event.user, (self.agent, self.agent2))
        other = self.agent2 if event.user == self.agent else self.agent
        self.assertTrue(self.hot(event.user)[0]["ring"])
        self.assertEqual((self.hot(other)[0]["ring"], self.hot(other)[0]["can_call"]), (False, False))
        self.assertIsNone(event.answered_at)                                       # the banner stays until Call

    def test_owners_are_alerted_once_when_nobody_called_within_10_minutes(self, _):
        admin = User.objects.create_user(email="admin@calls.test", password="pw-123456789", organization=self.org,
                                         role=User.Role.ADMIN, first_name="Ada")
        conv = self.conv("CA")
        self.now_call(conv)
        event = CalendarEvent.objects.get(conversation=conv)
        self.sent.clear()
        send_due_reminders(now=event.start_at + timedelta(minutes=9))
        self.assertFalse([p for _, p in self.sent if p.get("reminder", {}).get("stage") == "escalated"])
        send_due_reminders(now=event.start_at + timedelta(minutes=10))
        told = {uid for uid, p in self.sent if p.get("reminder", {}).get("stage") == "escalated"}
        self.assertEqual(told, {self.owner.id, admin.id})
        self.assertIn("Nobody has called this hot lead", self.call_notes_any(conv))
        self.sent.clear()
        send_due_reminders(now=event.start_at + timedelta(minutes=12))
        self.assertFalse([p for _, p in self.sent if p.get("reminder", {}).get("stage") == "escalated"])

    def test_an_answered_hot_lead_is_never_escalated_or_reminded(self, _):
        conv = self.conv("CA")
        self.now_call(conv)
        event = CalendarEvent.objects.get(conversation=conv)
        calls.answer_call(event, self.agent)
        self.sent.clear()
        send_due_reminders(now=event.start_at + timedelta(minutes=15))
        self.assertFalse([p for _, p in self.sent if p.get("event") == "call_reminder"])

    def test_a_lead_with_an_agent_rings_only_that_agent(self, _):
        conv = self.conv("CA", assigned=self.agent2)
        self.now_call(conv)
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.user, self.agent2)
        self.assertTrue(self.hot(self.agent2)[0]["ring"])
        lead = self.hot(self.agent)[0]
        self.assertEqual((lead["ring"], lead["can_call"], lead["waiting_for"]), (False, False, "Bea"))

    def test_call_me_now_moves_an_already_booked_call_to_now(self, _):
        conv = self.conv("CA")
        self.book_tomorrow_10(conv)
        self.now_call(conv, "actually can you call me now")
        [event] = CalendarEvent.objects.filter(conversation=conv)
        self.assertTrue(event.urgent)
        self.assertLess(event.start_at - timezone.now(), timedelta(minutes=2))

    def test_not_now_books_nothing(self, _):
        conv = self.conv("CA")
        self.say(conv, "not now, i'm busy", "No problem!", needs_human=False)
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())

    def test_another_workspace_cannot_answer(self, _):
        conv = self.conv("CA")
        self.now_call(conv)
        event = CalendarEvent.objects.get(conversation=conv)
        other = Organization.objects.create(name="Other", slug="other-hot")
        stranger = User.objects.create_user(email="z@other.test", password="pw-123456789", organization=other)
        self.client.force_login(stranger)
        self.assertEqual(self.client.post(f"/calls/hot/{event.id}/answer/").status_code, 404)
        self.assertEqual(self.client.get("/calls/hot/").json()["leads"], [])

    def test_every_page_has_the_banner_and_ringing(self, _):
        self.client.force_login(self.agent)
        html = self.client.get("/calendar/").content.decode()
        self.assertIn('id="hot-lead-banner"', html)
        self.assertIn("urgent_refresh", html)

    # ---- after 10 minutes nobody called: owners and admins ring too (works without the live connection)
    def escalate(self, event):
        send_due_reminders(now=event.start_at + timedelta(minutes=10))
        event.refresh_from_db()
        return event

    def test_after_10_minutes_owners_and_admins_ring_even_if_they_ignored_it(self, _):
        admin = User.objects.create_user(email="adm@calls.test", password="pw-123456789", organization=self.org,
                                         role=User.Role.ADMIN, first_name="Ada")
        conv = self.conv("CA")
        self.now_call(conv)
        event = CalendarEvent.objects.get(conversation=conv)
        calls.ignore_call(event, admin)                      # Ada (available) ignored it at first
        self.assertFalse(self.hot(self.owner)[0]["ring"])    # the owner isn't available: banner only
        self.assertFalse(self.hot(admin)[0]["ring"])
        self.assertIsNotNone(self.escalate(event).escalated_at)
        for boss in (self.owner, admin):
            lead = self.hot(boss)[0]
            self.assertEqual((lead["ring"], lead["escalated"], lead["can_call"]), (True, True, True))
        self.assertTrue(self.hot(self.agent)[0]["ring"])     # the agents keep ringing too

    def test_an_owner_can_take_over_a_hot_lead_nobody_called(self, _):
        conv = self.conv("CA", assigned=self.agent2)
        self.now_call(conv)
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(calls.answer_call(event, self.owner)[0], False)     # not before the 10 minutes
        self.escalate(event)
        self.assertEqual(calls.answer_call(event, self.owner), (True, ""))
        event.refresh_from_db()
        self.assertEqual(event.user, self.owner)
        self.assertEqual(self.hot(self.agent2), [])

    def test_the_bell_shows_the_escalation_after_a_reload(self, _):
        conv = self.conv("CA")
        self.now_call(conv)
        self.escalate(CalendarEvent.objects.get(conversation=conv))
        self.client.force_login(self.owner)
        html = self.client.get("/calendar/").content.decode()
        self.assertIn("Hot lead still waiting", html)              # in the bell's starting list
        self.client.force_login(self.agent)                        # agents aren't escalated to
        self.assertNotIn("Hot lead still waiting", self.client.get("/calendar/").content.decode())

    # ---- ringing starts the moment the text arrives (before the AI reply)
    def inbound(self, conv, body, sid):
        with self.settings(TWILIO_VALIDATE_WEBHOOKS=True):
            return self.client.post("/webhooks/twilio/sms/", {"From": conv.contact.phone, "To": self.phone.e164,
                                                              "Body": body, "MessageSid": sid},
                                    HTTP_X_TWILIO_SIGNATURE="valid_test_signature")

    def run_pending_job(self, conv, reply="Sure!"):
        from messaging.ai.fake_client import FakeAIClient
        from messaging.ai.pipeline import process_ai_job
        from messaging.models import AIJob
        FakeAIClient.push_response({"reply": reply, "intent": "answer", "collected": {}, "needs_human": True,
                                    "handoff_reason": "Wants a call", "checks": {}})
        job = AIJob.objects.get(conversation=conv, status=AIJob.Status.PENDING)
        process_ai_job(job)
        conv.refresh_from_db()

    def test_ringing_starts_the_moment_the_text_arrives(self, _):
        conv = self.conv("CA")
        self.assertEqual(self.inbound(conv, "can you call me now?", "SM_hot_1").status_code, 200)
        event = CalendarEvent.objects.get(conversation=conv)                 # before the AI has replied
        self.assertTrue(event.urgent)
        self.assertLess(abs((event.start_at - timezone.now()).total_seconds()), 5)   # the moment they asked
        [lead] = self.hot(self.agent)
        self.assertTrue(lead["ring"])
        self.assertEqual(lead["asked_at_ms"], int(event.start_at.timestamp() * 1000))
        self.assertIn("server_now_ms", self.client.get("/calls/hot/").json())
        # then Gemma's reply goes out, and the call isn't booked twice or restarted
        self.run_pending_job(conv, "Sure, I'll get someone to call you right now!")
        self.assertEqual(CalendarEvent.objects.filter(conversation=conv).count(), 1)
        reply = Message.objects.filter(conversation=conv, sender_type=Message.SenderType.AI).last().body
        self.assertIn("few minutes", reply)
        self.assertEqual(conv.status, Conversation.Status.NEEDS_HUMAN)
        self.assertEqual(Message.objects.filter(conversation=conv, body__startswith="🔥 Hot lead").count(), 1)

    def test_a_call_taken_before_the_ai_replies_stays_taken(self, _):
        conv = self.conv("CA")
        self.inbound(conv, "call me now please", "SM_hot_2")
        event = CalendarEvent.objects.get(conversation=conv)
        calls.answer_call(event, self.agent2)
        self.run_pending_job(conv)
        event.refresh_from_db()
        self.assertEqual(event.user, self.agent2)
        self.assertIsNotNone(event.answered_at)                              # not ringing again
        self.assertEqual(self.hot(self.agent), [])

    def test_a_bare_now_out_of_the_blue_does_not_ring(self, _):
        conv = self.conv("CA")
        self.inbound(conv, "now", "SM_hot_3")
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())

    def test_stop_never_rings(self, _):
        conv = self.conv("CA")
        self.inbound(conv, "STOP", "SM_hot_4")
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())

    def call_notes_any(self, conv):
        return " ".join(Message.objects.filter(conversation=conv, sender_type=Message.SenderType.SYSTEM)
                        .values_list("body", flat=True))


class NobodyCanCallNowTests(TeamCallTestCase):
    @patch("scheduling.calls.can_call_now", return_value=(False, "our agents aren't available to call right now"))
    def test_the_lead_hears_the_truth_and_the_next_opening(self, _):
        conv = self.conv("CA")
        reply = self.say(conv, "can you call me now?", "Sure, calling you now!")
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertIn("aren't available to call right now", reply)
        self.assertIn("?", reply)
        self.assertIn("🔥 The lead asked for a call right now", self.call_notes_any(conv))
        out = conv.ai_replies.order_by("-created_at").first().parsed_output
        self.assertTrue(out.get("call_proposal"))                                  # "yes" books the suggestion

    def call_notes_any(self, conv):
        return " ".join(Message.objects.filter(conversation=conv, sender_type=Message.SenderType.SYSTEM)
                        .values_list("body", flat=True))
