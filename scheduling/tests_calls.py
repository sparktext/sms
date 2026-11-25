"""Call scheduling: Gemma booking calls, the Schedule-call window, and call reminders."""
import json
from datetime import datetime, time as time_cls, timedelta, timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from contacts.models import Contact
from core.models import Organization, User
from messaging.ai.fake_client import FakeAIClient
from messaging.ai.pipeline import process_ai_job
from messaging.models import AIAssistantConfig, AIJob, Conversation, Message, PhoneNumber
from scheduling import calls
from scheduling.models import CalendarEvent
from scheduling.reminders import send_due_reminders

UTC = dt_timezone.utc
LA, NY = ZoneInfo("America/Los_Angeles"), ZoneInfo("America/New_York")


class Base(TestCase):
    def setUp(self):
        FakeAIClient.reset()
        self.org = Organization.objects.create(name="Pacific Test", slug="pacific-test", timezone="America/Los_Angeles",
                                               call_days=[0, 1, 2, 3, 4, 5, 6],   # tests run on any weekday
                                               call_start=time_cls(0, 0), call_end=time_cls(23, 59))  # only the
                                               # lead's legal 8 AM-9 PM limits calls unless a test sets team hours
        self.owner = User.objects.create_user(email="owner@calls.test", password="pw-123456789", organization=self.org,
                                              role=User.Role.OWNER, first_name="Olivia", is_available_for_leads=False)
        self.agent = User.objects.create_user(email="agent@calls.test", password="pw-123456789", organization=self.org,
                                              role=User.Role.AGENT, first_name="Aaron")
        self.phone = PhoneNumber.objects.create(organization=self.org, e164="+16505550199", is_active=True)
        self.n = 0
        self.sent = []          # live notifications that went out: (user_id, payload)
        patcher = patch("messaging.ws_broadcast.broadcast_user_event",
                        side_effect=lambda uid, payload: self.sent.append((uid, payload)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def conv(self, state="CA", assigned=None, status=Conversation.Status.OPEN):
        self.n += 1
        contact = Contact.objects.create(organization=self.org, phone=f"+1650555{1000 + self.n}", first_name="Maria",
                                         last_name="Lopez", sms_status=Contact.SMSStatus.OPTED_IN,
                                         custom_fields={"state": state} if state else {})
        return Conversation.objects.create(organization=self.org, contact=contact, phone_number=self.phone,
                                           assigned_to=assigned, status=status)

    def call_notes(self, conv):
        return list(Message.objects.filter(conversation=conv, sender_type=Message.SenderType.SYSTEM,
                                           body__contains="📅").values_list("body", flat=True))

    def reminders_sent(self, stage=None):
        return [p["reminder"] for _, p in self.sent if p.get("event") == "call_reminder"
                and (stage is None or p["reminder"]["stage"] == stage)]


# ---------------------------------------------------------------------------
# Lead time zone & reading "when"
# ---------------------------------------------------------------------------

class LeadTimeTests(Base):
    def test_lead_time_zone_comes_from_their_state(self):
        self.assertEqual(calls.lead_timezone(self.conv("CA").contact), "America/Los_Angeles")
        self.assertEqual(calls.lead_timezone(self.conv("New York").contact), "America/New_York")
        nested = self.conv(None).contact
        nested.custom_fields = {"applicant": {"address": {"State": "tx"}}}
        self.assertEqual(calls.lead_timezone(nested), "America/Chicago")
        self.assertEqual(calls.lead_timezone(self.conv(None).contact), "America/Los_Angeles")   # workspace zone

    def test_reading_when_into_a_moment(self):
        now = datetime(2026, 11, 21, 10, 15, tzinfo=LA)          # Saturday 10:15 AM in California
        tz = "America/Los_Angeles"

        def at(when):
            return calls.resolve_call_time(when, tz, now)

        self.assertEqual(at({"date": "2026-11-22", "time": "09:00"}), (datetime(2026, 11, 22, 9, 0, tzinfo=LA), "exact"))
        self.assertEqual(at({"time": "09:00"})[0], datetime(2026, 11, 22, 9, 0, tzinfo=LA))    # 9 AM passed: tomorrow
        self.assertEqual(at({"time": "15:00"})[0], datetime(2026, 11, 21, 15, 0, tzinfo=LA))   # later today
        self.assertEqual(at({"date": "2026-11-23", "part_of_day": "afternoon"}),
                         (datetime(2026, 11, 23, 14, 0, tzinfo=LA), "their_day"))
        self.assertEqual(at({"date": "2026-11-23"})[0], datetime(2026, 11, 23, 10, 0, tzinfo=LA))
        self.assertEqual(at({"asap": True}), (datetime(2026, 11, 21, 10, 30, tzinfo=LA), "asap"))
        self.assertEqual(at({})[1], "asap")                       # wants a call, no time given
        self.assertIsNone(at({"date": "2026-11-20", "time": "09:00"})[0])                     # already passed
        self.assertIsNone(at({"date": "2027-06-01", "time": "09:00"})[0])                     # too far out

    def test_a_bare_hour_picks_the_next_sensible_time(self):
        tz = "America/Los_Angeles"

        def bare(hour, now, **extra):
            return calls.resolve_call_time({"time": f"{hour:02d}:00", "am_pm_said": False, **extra}, tz, now)[0]

        lunch = datetime(2026, 11, 20, 12, 44, tzinfo=LA)
        self.assertEqual(bare(11, lunch), datetime(2026, 11, 21, 11, 0, tzinfo=LA))   # not 11 PM tonight
        afternoon = datetime(2026, 11, 20, 14, 0, tzinfo=LA)
        self.assertEqual(bare(7, afternoon), datetime(2026, 11, 20, 19, 0, tzinfo=LA))  # 7 PM today
        morning = datetime(2026, 11, 20, 10, 0, tzinfo=LA)
        self.assertEqual(bare(3, morning), datetime(2026, 11, 20, 15, 0, tzinfo=LA))    # 3 PM, not 3 AM
        self.assertEqual(bare(12, morning), datetime(2026, 11, 20, 12, 0, tzinfo=LA))   # noon
        self.assertEqual(bare(9, morning, date="2026-11-23"), datetime(2026, 11, 23, 9, 0, tzinfo=LA))
        self.assertEqual(calls.proposal_text(datetime(2026, 11, 21, 11, 0, tzinfo=LA), tz, lunch),
                         "11 AM tomorrow (Sat, Nov 21)")
        self.assertTrue(calls.am_pm_obvious(10))       # 10 PM is too late to call: 10 AM
        self.assertTrue(calls.am_pm_obvious(3))        # 3 AM is too early: 3 PM
        self.assertTrue(calls.am_pm_obvious(12))       # noon
        self.assertFalse(calls.am_pm_obvious(8))       # 8 AM and 8 PM both work: ask
        self.assertEqual(calls.proposal_text(datetime(2026, 11, 23, 14, 30, tzinfo=LA), tz, lunch),
                         "2:30 PM on Mon, Nov 23")

    def test_late_asap_waits_for_the_morning_and_a_new_york_lead_gets_their_9am(self):
        late = datetime(2026, 11, 21, 20, 50, tzinfo=LA)
        self.assertEqual(calls.next_call_slot(late, "America/Los_Angeles"), datetime(2026, 11, 22, 9, 0, tzinfo=LA))
        moment, _ = calls.resolve_call_time({"date": "2026-11-22", "time": "09:00"}, "America/New_York",
                                            datetime(2026, 11, 21, 12, 0, tzinfo=UTC))
        self.assertEqual(moment.astimezone(UTC), datetime(2026, 11, 22, 14, 0, tzinfo=UTC))

    def test_daylight_saving_lead_time(self):
        moment, _ = calls.resolve_call_time({"date": "2027-03-15", "time": "09:00"}, "America/Los_Angeles",
                                            datetime(2027, 3, 10, 12, 0, tzinfo=UTC))
        self.assertEqual(moment.astimezone(UTC), datetime(2027, 3, 15, 16, 0, tzinfo=UTC))   # PDT


# ---------------------------------------------------------------------------
# Gemma books the call
# ---------------------------------------------------------------------------

@override_settings(MESSAGING_PROVIDER="fake", AI_CLIENT="fake")
class GemmaBooksCallTests(Base):
    def setUp(self):
        super().setUp()
        AIAssistantConfig.objects.create(organization=self.org, business_name="Pacific Test",
                                         mode=AIAssistantConfig.Mode.AUTO)

    def tomorrow(self, tz=LA):
        return (timezone.now().astimezone(tz) + timedelta(days=1)).date()

    def run_ai(self, conv, text, reply, read=None, needs_human=True, reason="Wants a call - best time: tomorrow at 9am"):
        Message.objects.create(organization=self.org, conversation=conv, direction=Message.Direction.OUTBOUND,
                               sender_type=Message.SenderType.AI, status=Message.Status.DELIVERED,
                               body="Want a licensed agent to call you with options? What time works?")
        msg = Message.objects.create(organization=self.org, conversation=conv, direction=Message.Direction.INBOUND,
                                     sender_type=Message.SenderType.CUSTOMER, body=text, status=Message.Status.RECEIVED)
        FakeAIClient.push_response({"reply": reply, "intent": "answer", "collected": {}, "needs_human": needs_human,
                                    "handoff_reason": reason if needs_human else None, "checks": {}})
        if read is not None:
            FakeAIClient.push_response({"wants_call": True, "date": "", "time": "", "part_of_day": "", "asap": False,
                                        "their_words": "", "day_said": True, "am_pm_said": True, **read})
        process_ai_job(AIJob.objects.create(conversation=conv, trigger_message=msg, status=AIJob.Status.PENDING,
                                            run_after=timezone.now()))
        conv.refresh_from_db()

    def test_lead_agrees_and_the_call_lands_on_the_assigned_agents_calendar(self):
        conv = self.conv("CA")
        day = self.tomorrow()
        self.run_ai(conv, "yes please call me tomorrow at 9am", "Perfect, an agent will call you tomorrow at 9!",
                    read={"date": day.isoformat(), "time": "09:00", "their_words": "tomorrow at 9am"})

        self.assertEqual(conv.assigned_to, self.agent)
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.user, self.agent)
        self.assertEqual((event.kind, event.source, event.contact), ("call", "ai", conv.contact))
        self.assertEqual(event.start_at, datetime.combine(day, datetime.min.time().replace(hour=9), tzinfo=LA))
        self.assertEqual(event.reminder_minutes, CalendarEvent.DEFAULT_REMINDER_MINUTES)
        self.assertIn("tomorrow at 9am", event.notes)
        [note] = self.call_notes(conv)
        self.assertIn("Call booked by Gemma", note)
        self.assertIn("9:00 AM", note)
        [booked] = self.reminders_sent("booked")
        self.assertEqual((booked["assigned_to_id"], booked["url"]), (self.agent.id, f"/inbox/{conv.id}/"))

    def test_new_york_lead_is_called_at_their_9am(self):
        conv = self.conv("NY")
        day = self.tomorrow(NY)
        self.run_ai(conv, "call me tomorrow 9am", "Great, talk tomorrow at 9!",
                    read={"date": day.isoformat(), "time": "09:00", "their_words": "tomorrow 9am"})
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.start_at.astimezone(NY).hour, 9)
        self.assertEqual(event.start_at.astimezone(LA).hour, 6)    # what a California agent sees

    def test_lead_moves_the_booked_call(self):
        conv = self.conv("CA", assigned=self.agent, status=Conversation.Status.NEEDS_HUMAN)
        conv.handoff_reason = "Wants a call - best time: tomorrow at 9am"
        conv.save()
        day = self.tomorrow()
        event, _ = calls.book_call(contact=conv.contact, user=self.agent, conversation=conv,
                                   start=datetime.combine(day, datetime.min.time().replace(hour=9), tzinfo=LA),
                                   source=CalendarEvent.Source.AI)
        CalendarEvent.objects.filter(pk=event.pk).update(reminder_sent_at=timezone.now())
        self.run_ai(conv, "can we do 4pm instead?", "Sure, 4pm tomorrow works!",
                    read={"date": day.isoformat(), "time": "16:00", "their_words": "4pm instead"},
                    reason="Customer asked question while awaiting agent")

        moved = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(moved.pk, event.pk)
        self.assertEqual(moved.start_at.astimezone(LA).hour, 16)
        self.assertIsNone(moved.reminder_sent_at)                  # reminders go out again for the new time
        self.assertIn("Call moved by Gemma", self.call_notes(conv)[-1])
        [moved_note] = self.reminders_sent("moved")
        self.assertTrue(moved_note["body"].startswith("Moved: Maria Lopez is now"))
        # the handoff banner / alerts show the new time, not the old "tomorrow at 9am"
        self.assertIn("booked for", conv.handoff_reason)
        self.assertIn("4:00 PM", conv.handoff_reason)

    def last_ai_text(self, conv):
        return Message.objects.filter(conversation=conv, sender_type=Message.SenderType.AI).order_by(
            "-created_at").values_list("body", flat=True).first()

    def test_unclear_11_is_checked_with_the_lead_before_booking(self):
        conv = self.conv("CA")
        # "11" with no day and no am/pm
        self.run_ai(conv, "11 works for me", "Perfect, an agent will call you at 11:00!",
                    read={"time": "11:00", "their_words": "11", "day_said": False, "am_pm_said": False},
                    reason="Wants a call - best time: 11:00")
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertEqual(conv.status, Conversation.Status.OPEN)          # not handed off until it's clear
        question = self.last_ai_text(conv)
        self.assertTrue(question.startswith("Just to confirm, is "), question)
        self.assertRegex(question, r"\b11 (AM|PM) (today|tomorrow) \(\w{3}, \w{3} \d{1,2}\)")
        proposal = conv.ai_replies.order_by("-created_at").first().parsed_output["call_proposal"]

        # "yes" -> the time we suggested is booked and the chat goes to an agent
        self.run_ai(conv, "yes", "Great!", read={}, needs_human=False)
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.start_at, datetime.fromisoformat(proposal))
        self.assertEqual(event.start_at.astimezone(LA).hour, 11)
        self.assertEqual(conv.status, Conversation.Status.NEEDS_HUMAN)
        self.assertIn("booked for", conv.handoff_reason)
        confirmed = self.last_ai_text(conv)
        self.assertIn("11 AM", confirmed)                              # the reply says the day and time

    def test_what_the_lead_said_decides_not_the_models_guesses(self):
        # "at 3pm": no day named (the model claims it was, and guesses today) -> check first
        conv = self.conv("CA")
        self.run_ai(conv, "at 3pm", "Perfect, talk at 3!", read={"date": timezone.now().astimezone(LA).date().isoformat(),
                                                                "time": "15:00", "their_words": "at 3pm"})
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertIn("3 PM", self.last_ai_text(conv))
        # "no, tomorrow": keeps our 3 PM (the model dropped it to 03:00), changes the day, and books
        tomorrow = self.tomorrow()
        self.run_ai(conv, "no, tomorrow", "Ok!", read={"date": tomorrow.isoformat(), "time": "03:00",
                                                      "their_words": "tomorrow"}, needs_human=False)
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.start_at, datetime.combine(tomorrow, datetime.min.time().replace(hour=15), tzinfo=LA))

        # "11": the model turned it into 23:00 -> back to 11, code picks the slot and asks
        conv2 = self.conv("CA")
        self.run_ai(conv2, "11 works for me", "Perfect!", read={"time": "23:00", "their_words": "11"})
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv2).exists())
        self.assertRegex(self.last_ai_text(conv2), r"\b11 AM\b")

    def test_answering_our_suggestion_with_a_new_time_keeps_the_day(self):
        conv = self.conv("CA")
        tomorrow = self.tomorrow()
        self.run_ai(conv, "tomorrow at 8", "Sure!", read={"date": tomorrow.isoformat(), "time": "08:00",
                                                         "their_words": "tomorrow at 8"})
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())   # 8 AM or 8 PM? -> asked
        self.assertIn("8 AM tomorrow", self.last_ai_text(conv))
        # "8pm please" (model left the date out): same day we suggested, now at 8 PM -> booked
        self.run_ai(conv, "8pm please", "Got it!", read={"time": "20:00", "their_words": "8pm"}, needs_human=False)
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.start_at, datetime.combine(tomorrow, datetime.min.time().replace(hour=20), tzinfo=LA))
        self.assertNotIn("?", self.last_ai_text(conv))                 # a confirmation, not another question

    def test_a_past_date_is_never_promised_to_the_lead(self):
        conv = self.conv("CA", assigned=self.agent, status=Conversation.Status.NEEDS_HUMAN)
        monday = self.tomorrow() + timedelta(days=2)
        start = datetime.combine(monday, datetime.min.time().replace(hour=11), tzinfo=LA)
        calls.book_call(contact=conv.contact, user=self.agent, conversation=conv, start=start,
                        source=CalendarEvent.Source.AI)
        yesterday = (timezone.now().astimezone(LA) - timedelta(days=1)).date()
        msg_text = f"hey can we make it for 10 am, on {yesterday.day} {yesterday:%b}".lower()
        # Gemma's first reply AND its rewrite both claim the change: neither may reach the lead
        Message.objects.create(organization=self.org, conversation=conv, direction=Message.Direction.INBOUND,
                               sender_type=Message.SenderType.CUSTOMER, body=msg_text, status=Message.Status.RECEIVED)
        trigger = Message.objects.filter(conversation=conv).order_by("-created_at").first()
        FakeAIClient.push_response({"reply": "Sure! I've updated your call to 10 AM.", "intent": "answer",
                                    "collected": {}, "needs_human": True, "handoff_reason": "Wants a call", "checks": {}})
        FakeAIClient.push_response({"wants_call": True, "date": yesterday.isoformat(), "time": "10:00",
                                    "day_said": True, "am_pm_said": True, "part_of_day": "", "asap": False,
                                    "their_words": msg_text})
        FakeAIClient.push_response({"reply": "Done, all set for 10 AM!", "intent": "answer", "collected": {},
                                    "needs_human": True, "handoff_reason": None, "checks": {}})
        process_ai_job(AIJob.objects.create(conversation=conv, trigger_message=trigger, status=AIJob.Status.PENDING,
                                            run_after=timezone.now()))

        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.start_at, start)                       # booking untouched
        reply = self.last_ai_text(conv)
        self.assertIn("already passed", reply)
        self.assertIn("still set for 11 AM", reply)
        self.assertTrue(reply.endswith("?"))
        self.assertNotRegex(reply.lower(), r"updated|all set for 10")
        self.assertIn("already passed", self.call_notes(conv)[-1])    # and the agent is told

    def test_no_to_our_suggestion_books_nothing(self):
        conv = self.conv("CA")
        self.run_ai(conv, "11 works", "Ok!", read={"time": "11:00", "their_words": "11"})
        self.run_ai(conv, "no, next week sometime", "No problem!", read={"their_words": "next week sometime"},
                    needs_human=False)
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertIn("couldn't be pinned", self.call_notes(conv)[-1])

    def test_a_clear_time_is_booked_straight_away_and_the_reply_says_when(self):
        conv = self.conv("CA")
        day = self.tomorrow()
        self.run_ai(conv, "tomorrow at 9am", "Perfect, talk soon!",
                    read={"date": day.isoformat(), "time": "09:00", "their_words": "tomorrow at 9am"})
        self.assertTrue(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertIn("9 AM tomorrow", self.last_ai_text(conv))         # vague reply replaced by a clear one

    def test_thanks_after_booking_leaves_the_call_alone(self):
        conv = self.conv("CA", assigned=self.agent, status=Conversation.Status.NEEDS_HUMAN)
        start = datetime.combine(self.tomorrow(), datetime.min.time().replace(hour=9), tzinfo=LA)
        event, _ = calls.book_call(contact=conv.contact, user=self.agent, conversation=conv, start=start,
                                   source=CalendarEvent.Source.AI)
        # Gemma repeats "Wants a call..." on the follow-up; the lead didn't name a time
        self.run_ai(conv, "thanks!", "You're welcome!", read={}, reason="Wants a call - best time: tomorrow at 9am")
        event.refresh_from_db()
        self.assertEqual(event.start_at, start)
        self.assertFalse(any("wants_call" in ((c.get("schema") or {}).get("properties") or {})
                             for c in FakeAIClient.calls))

    def soon(self, minutes=20):
        return (timezone.now() + timedelta(minutes=minutes)).replace(second=0, microsecond=0)

    def test_yes_without_a_time_books_when_an_agent_can_call_soon(self):
        conv = self.conv("CA")
        slot = self.soon()
        with patch("scheduling.calls.next_call_slot", return_value=slot),                 patch("scheduling.calls.can_call_at", return_value=True):
            self.run_ai(conv, "yes", "Great, an agent will call you shortly!", read={},
                        reason="Wants a call - best time: not given")
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.start_at, slot)
        self.assertIn("didn't give a time", event.notes)

    def test_yes_at_night_suggests_the_time_instead_of_booking_it(self):
        # Live: "ok" at 1 AM booked 9 AM without asking. Now Gemma suggests 9 AM and lets them pick.
        conv = self.conv("CA")
        nine = datetime.combine(self.tomorrow(), datetime.min.time().replace(hour=9), tzinfo=LA)
        with patch("scheduling.calls.next_call_slot", return_value=nine):
            self.run_ai(conv, "ok", "Perfect, you're all set for a call at 9 AM!", read={},
                        reason="Wants a call - best time: not given")
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())     # nothing booked yet
        reply = self.last_ai_text(conv)
        self.assertIn("9 AM tomorrow", reply)
        self.assertIn("better time", reply)
        self.assertIn("?", reply)
        self.assertEqual(conv.status, Conversation.Status.OPEN)                       # not handed off yet
        # their "yes" books exactly the suggested time
        self.run_ai(conv, "yes", "Great!", needs_human=False)
        self.assertEqual(CalendarEvent.objects.get(conversation=conv).start_at, nine)

    def test_suggested_time_can_be_changed_by_the_lead(self):
        conv = self.conv("CA")
        nine = datetime.combine(self.tomorrow(), datetime.min.time().replace(hour=9), tzinfo=LA)
        with patch("scheduling.calls.next_call_slot", return_value=nine):
            self.run_ai(conv, "sure", "Great!", read={}, reason="Wants a call - best time: not given")
        self.run_ai(conv, "11am works better", "Ok!", needs_human=False,
                    read={"time": "11:00", "am_pm_said": True, "their_words": "11am"})
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.start_at.astimezone(LA).hour, 11)
        self.assertEqual(event.start_at.astimezone(LA).date(), self.tomorrow())    # same day as suggested

    def test_yes_read_as_their_words_is_not_a_vague_time(self):
        # Live: the time reader returned their_words "YES", which looked like a vague time -> only a note
        conv = self.conv("CA")
        with patch("scheduling.calls.next_call_slot", return_value=self.soon()),                 patch("scheduling.calls.can_call_at", return_value=True):
            self.run_ai(conv, "YES", "Awesome! What time works best?", read={"their_words": "YES"},
                        reason="Wants a call - best time: not given")
        self.assertTrue(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertFalse(any("couldn't be pinned" in n for n in self.call_notes(conv)))

    def test_a_written_date_wins_when_the_reader_drops_it(self):
        # Live: "call me march 15 at 10am" was read without the date and booked for 10 AM today
        conv = self.conv("CA")
        self.run_ai(conv, "call me march 15 at 10am", "Perfect, see you then!",
                    read={"time": "10:00", "their_words": "march 15 at 10am"})
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertIn("more than two months away", self.call_notes(conv)[-1])

    def test_an_unrelated_reply_never_books_the_suggested_time(self):
        # Live: Gemma suggested 9 AM, the lead answered "auto" (a question's answer) and 9 AM got booked
        conv = self.conv("CA")
        nine = datetime.combine(self.tomorrow(), datetime.min.time().replace(hour=9), tzinfo=LA)
        with patch("scheduling.calls.next_call_slot", return_value=nine):
            self.run_ai(conv, "sure", "Great!", read={}, reason="Wants a call - best time: not given")
        self.run_ai(conv, "auto", "Got it!", needs_human=False,
                    read={"date": self.tomorrow().isoformat(), "time": "09:00", "their_words": "auto"})
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())

    def test_today_at_night_suggests_the_morning(self):
        # "Today or tomorrow? Reply TODAY" answered at night: the earliest is 9 AM tomorrow, so it's suggested
        conv = self.conv("CA")
        nine = datetime.combine(self.tomorrow(), datetime.min.time().replace(hour=9), tzinfo=LA)
        today = timezone.now().astimezone(LA).date()
        with patch("scheduling.calls.next_call_slot", return_value=nine):
            self.run_ai(conv, "TODAY", "Awesome! What can we help you with today?",
                        read={"date": today.isoformat(), "their_words": "today"}, reason="Wants a call - best time: today")
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        reply = self.last_ai_text(conv)
        self.assertIn("9 AM tomorrow", reply)
        self.assertIn("?", reply)

    def campaign_reply(self, conv, offer, text, gemma_reply):
        Message.objects.create(organization=self.org, conversation=conv, direction=Message.Direction.OUTBOUND,
                               sender_type=Message.SenderType.CAMPAIGN, status=Message.Status.DELIVERED, body=offer)
        msg = Message.objects.create(organization=self.org, conversation=conv, direction=Message.Direction.INBOUND,
                                     sender_type=Message.SenderType.CUSTOMER, body=text, status=Message.Status.RECEIVED)
        FakeAIClient.push_response({"reply": gemma_reply, "intent": "answer", "collected": {}, "needs_human": False,
                                    "handoff_reason": None, "checks": {}})
        process_ai_job(AIJob.objects.create(conversation=conv, trigger_message=msg, status=AIJob.Status.PENDING,
                                            run_after=timezone.now()))
        conv.refresh_from_db()

    def test_yes_to_a_campaign_call_offer_moves_to_the_call_instead_of_qualifying(self):
        AIAssistantConfig.objects.filter(organization=self.org).update(qualification_questions=[
            {"key": "zip_code", "question": "What's your ZIP code?", "required": True}])
        conv = self.conv("CA")
        offer = ("Hi Maria, Emma (virtual assistant) from Pacific Haven. Many drivers find discounts they didn't know "
                 "about. Want an agent to check yours? Reply YES. STOP to opt out.")
        nine = datetime.combine(self.tomorrow(), datetime.min.time().replace(hour=9), tzinfo=LA)
        with patch("scheduling.calls.next_call_slot", return_value=nine):            # at night: suggested first
            self.campaign_reply(conv, offer, "YES", "Awesome! To get started, what can we help you with today?")
        reply = self.last_ai_text(conv)
        self.assertIn("9 AM tomorrow", reply)
        self.assertNotIn("ZIP", reply)
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        conv2 = self.conv("CA")
        slot = self.soon()
        with patch("scheduling.calls.next_call_slot", return_value=slot),                 patch("scheduling.calls.can_call_at", return_value=True):                # daytime: booked right away
            self.campaign_reply(conv2, offer, "YES", "Awesome! To get started, what can we help you with today?")
        event = CalendarEvent.objects.get(conversation=conv2)
        self.assertEqual((event.start_at, event.user), (slot, self.agent))
        self.assertNotIn("ZIP", self.last_ai_text(conv2))
        self.assertEqual(conv2.status, Conversation.Status.NEEDS_HUMAN)

    def test_ok_to_a_statement_about_an_agent_books_nothing(self):
        conv = self.conv("CA")
        self.campaign_reply(conv, "Thanks Maria! Our agent will reach out with your quote.", "ok", "Sounds good!")
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())

    # ---- call days & calling hours
    def next_weekday(self, weekday):
        today = timezone.now().astimezone(LA).date()
        return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)

    def test_a_day_the_team_doesnt_call_is_declined_and_another_time_asked(self):
        self.org.call_days = [0, 1, 2, 3, 4]
        self.org.save()
        conv = self.conv("CA")
        sat = self.next_weekday(5)
        self.run_ai(conv, "can you call me saturday at 11am?", "Sure, Saturday at 11 works!",
                    read={"date": sat.isoformat(), "time": "11:00", "their_words": "saturday at 11am"})
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertNotEqual(self.last_ai_text(conv), "Sure, Saturday at 11 works!")
        self.assertIn("outside your team's call hours", self.call_notes(conv)[-1])
        self.assertIn("Monday-Friday", self.call_notes(conv)[-1])

    def test_saturday_is_booked_when_the_team_calls_on_saturdays(self):
        self.org.call_days = [0, 1, 2, 3, 4, 5]
        self.org.save()
        conv = self.conv("CA")
        sat = self.next_weekday(5)
        self.run_ai(conv, "can you call me saturday at 11am?", "Sure!",
                    read={"date": sat.isoformat(), "time": "11:00", "their_words": "saturday at 11am"})
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.start_at, datetime.combine(sat, datetime.min.time().replace(hour=11), tzinfo=LA))

    def test_a_time_outside_calling_hours_is_declined(self):
        conv = self.conv("CA")
        self.run_ai(conv, "call me tomorrow at 11pm", "Sure, 11pm it is!",
                    read={"date": self.tomorrow().isoformat(), "time": "23:00", "their_words": "tomorrow at 11pm"})
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertIn("outside the legal calling window", self.call_notes(conv)[-1])

    def test_next_open_slot_skips_days_without_calls(self):
        self.org.call_days = [0, 1, 2, 3, 4]
        self.org.call_start, self.org.call_end = time_cls(8, 0), time_cls(21, 0)
        friday_night = datetime(2026, 11, 27, 21, 30, tzinfo=LA)
        self.assertEqual(calls.next_call_slot(friday_night, "America/Los_Angeles", self.org),
                         datetime(2026, 11, 30, 9, 0, tzinfo=LA))                       # Monday 9 AM
        self.assertTrue(calls.team_hours_text(self.org).startswith("8 AM-9 PM"))
        self.assertTrue(calls.team_hours_text(self.org).endswith("Monday-Friday"))

    # ---- what the lead wrote wins
    def test_lead_writing_pm_wins_over_a_misread_time(self):
        conv = self.conv("CA")
        day = self.tomorrow()
        self.run_ai(conv, "TOMORROW", "Great!", read={"date": day.isoformat(), "their_words": "TOMORROW"},
                    needs_human=False)
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())    # asked to confirm 10 AM
        # Live, Gemma read "no, make it 4pm" as 04:00
        self.run_ai(conv, "no, make it 4pm", "Ok!", needs_human=False,
                    read={"date": day.isoformat(), "time": "04:00", "am_pm_said": True, "their_words": "no, make it 4pm"})
        self.assertEqual(CalendarEvent.objects.get(conversation=conv).start_at.astimezone(LA).hour, 16)

    # ---- cancelling
    def booked_conv(self):
        conv = self.conv("CA", assigned=self.agent, status=Conversation.Status.NEEDS_HUMAN)
        conv.handoff_reason = "Wants a call - booked"
        conv.save()
        start = datetime.combine(self.tomorrow(), datetime.min.time().replace(hour=11), tzinfo=LA)
        event, _ = calls.book_call(contact=conv.contact, user=self.agent, conversation=conv, start=start,
                                   source=CalendarEvent.Source.AI)
        return conv, event

    def test_lead_cancels_the_booked_call(self):
        conv, event = self.booked_conv()
        self.run_ai(conv, "actually please cancel the call", "Sure, I've cancelled that for you!",
                    reason="Customer asked to cancel")
        event.refresh_from_db()
        conv.refresh_from_db()
        self.assertEqual(event.status, CalendarEvent.Status.CANCELLED)
        self.assertIn("cancel", self.last_ai_text(conv).lower())
        self.assertIn("Call cancelled", self.call_notes(conv)[-1])
        self.assertEqual(conv.handoff_reason, "Call cancelled by the lead")
        [note] = self.reminders_sent("cancelled")
        self.assertTrue(note["body"].startswith("Cancelled: Maria Lopez"))
        self.assertIsNone(calls.upcoming_call(conversation=conv))

    def test_gemma_never_claims_a_cancel_that_didnt_happen(self):
        conv, event = self.booked_conv()
        # Names a time, so it isn't read as a plain cancel; Gemma claims it anyway
        self.run_ai(conv, "can you cancel the 11am one?", "Done, I've cancelled it!", reason="Customer question")
        event.refresh_from_db()
        self.assertEqual(event.status, CalendarEvent.Status.SCHEDULED)
        self.assertNotIn("cancelled", self.last_ai_text(conv).lower())

    def test_what_counts_as_our_call_offer(self):
        from messaging.ai.pipeline import _AGENT_OFFER_RE, _is_call_offer
        offer = lambda t: _is_call_offer(t) or bool(_AGENT_OFFER_RE.search(t))       # noqa: E731
        for text in ("Can an agent call for 5 min to check which discounts you qualify for? Reply YES",
                     "An agent can check which discounts you may qualify for in a quick call. Today or tomorrow?",
                     "Many drivers find discounts they didn't know about. Want an agent to check yours? Reply YES.",
                     "Would you like a licensed agent to go over your options?"):
            self.assertTrue(offer(text), text)
        for text in ("Are you currently working with an agent?", "Would you like to keep your current agent?",
                     "Do you already have an insurance agent?", "What's your ZIP code?"):
            self.assertFalse(offer(text), text)

    def test_vague_or_past_times_are_left_for_the_agent(self):
        conv = self.conv("CA")
        self.run_ai(conv, "sometime next week", "Sounds good!", read={"their_words": "next week"})
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertIn("couldn't be pinned", self.call_notes(conv)[-1])

        conv2 = self.conv("CA")
        yesterday = (timezone.now().astimezone(LA) - timedelta(days=1)).date()
        self.run_ai(conv2, "yesterday at 9", "Ok!", read={"date": yesterday.isoformat(), "time": "09:00",
                                                         "their_words": "yesterday at 9"})
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv2).exists())
        self.assertIn("already passed", self.call_notes(conv2)[-1])

    def test_nobody_assigned_goes_to_the_owner(self):
        self.org.lead_routing_strategy = Organization.LeadRoutingStrategy.DISABLED
        self.org.save()
        conv = self.conv("CA")
        self.run_ai(conv, "call me tomorrow at 10", "Will do!",
                    read={"date": self.tomorrow().isoformat(), "time": "10:00", "their_words": "tomorrow at 10"})
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.user, self.owner)
        self.assertIn("Nobody was assigned", event.notes)

    def test_ordinary_messages_dont_trigger_a_call_check(self):
        conv = self.conv("CA")
        self.run_ai(conv, "I drive a Honda", "Nice! What year is it?", needs_human=False)
        self.assertFalse(CalendarEvent.objects.exists())
        self.assertFalse(any("wants_call" in ((c.get("schema") or {}).get("properties") or {})
                             for c in FakeAIClient.calls))

    def test_gemma_is_told_about_the_booked_call(self):
        from messaging.ai.prompt import build_system_prompt
        conv = self.conv("NY", assigned=self.agent)
        calls.book_call(contact=conv.contact, user=self.agent, conversation=conv,
                        start=timezone.now() + timedelta(days=1))
        prompt = build_system_prompt(AIAssistantConfig.objects.get(organization=self.org), conv)
        self.assertIn("Booked call: a phone call with Aaron is booked", prompt)
        self.assertIn("(the customer's own time)", prompt)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

class CallReminderTests(Base):
    def event(self, minutes_from_now, reminder=10, user=None, **extra):
        conv = self.conv("CA", assigned=user or self.agent)
        start = timezone.now() + timedelta(minutes=minutes_from_now)
        event, _ = calls.book_call(contact=conv.contact, user=user or self.agent, conversation=conv, start=start,
                                   reminder_minutes=reminder)
        if extra:
            CalendarEvent.objects.filter(pk=event.pk).update(**extra)
            event.refresh_from_db()
        return event

    def test_heads_up_then_time_to_call_each_sent_once(self):
        event = self.event(8)
        self.assertEqual(send_due_reminders(), 1)
        self.assertEqual(send_due_reminders(), 0)
        [soon] = self.reminders_sent("soon")
        self.assertEqual(soon["assigned_to_id"], self.agent.id)
        self.assertIn("Maria Lopez in 8 min", soon["body"])
        self.assertEqual(soon["url"], f"/inbox/{event.conversation_id}/")

        self.assertEqual(send_due_reminders(now=event.start_at + timedelta(seconds=5)), 1)
        self.assertEqual(send_due_reminders(now=event.start_at + timedelta(seconds=20)), 0)
        self.assertEqual(len(self.reminders_sent("now")), 1)

    def test_not_yet_due_and_switched_off_reminders_stay_quiet(self):
        self.event(30)                                  # heads-up is 10 min before
        self.event(5, reminder=None)                    # no reminders
        self.event(3, reminder=0)                       # only when it starts
        self.event(5, status=CalendarEvent.Status.CANCELLED)
        self.event(-120)                                # started long ago (sender was off)
        self.assertEqual(send_due_reminders(), 0)

    def test_inactive_people_get_nothing(self):
        self.agent.is_active = False
        self.agent.save()
        self.event(5)
        self.assertEqual(send_due_reminders(), 0)

    def test_a_call_that_has_started_gets_time_to_call_not_a_late_heads_up(self):
        event = self.event(-1)
        self.assertEqual(send_due_reminders(), 1)
        event.refresh_from_db()
        self.assertIsNotNone(event.reminder_sent_at)
        self.assertEqual([r["stage"] for r in self.reminders_sent()], ["now"])

    def test_moving_a_call_sends_its_reminders_again(self):
        event = self.event(5)
        send_due_reminders()
        self.client.force_login(self.agent)
        new_start = (timezone.now() + timedelta(minutes=8)).astimezone(LA).replace(second=0, microsecond=0)
        res = self.client.post(f"/calendar/events/{event.id}/update/", data=json.dumps(
            {"start": new_start.isoformat(), "end": (new_start + timedelta(minutes=15)).isoformat()}),
            content_type="application/json")
        self.assertEqual(res.status_code, 200)
        event.refresh_from_db()
        self.assertIsNone(event.reminder_sent_at)
        self.assertEqual(send_due_reminders(), 1)

    def test_bell_shows_recent_reminders_after_a_reload(self):
        event = self.event(5)
        send_due_reminders()
        self.client.force_login(self.agent)
        res = self.client.get("/dashboard/")
        notifs = json.loads(res.context["initial_notifications_json"])
        self.assertTrue(any(n.get("type") == "call_reminder" and n["event_id"] == event.id for n in notifs))

    def test_page_warns_when_nothing_is_sending_reminders(self):
        banner = "Nothing is sending them right now"     # only in the calendar page's warning
        self.client.force_login(self.owner)
        self.assertContains(self.client.get("/calendar/"), banner)
        conv = self.conv("CA")
        self.assertFalse(self.client.get("/calendar/calls/options/", {"conversation": conv.id}).json()["reminders_running"])

        send_due_reminders()                            # the worker checks in
        self.assertNotContains(self.client.get("/calendar/"), banner)
        self.assertTrue(self.client.get("/calendar/calls/options/", {"conversation": conv.id}).json()["reminders_running"])

    def test_worker_once_mode_sends_due_reminders(self):
        from django.core.management import call_command
        self.event(5)
        call_command("run_ai_worker", "--once")
        self.assertEqual(len(self.reminders_sent("soon")), 1)


# ---------------------------------------------------------------------------
# "Schedule call" window
# ---------------------------------------------------------------------------

class ScheduleCallWindowTests(Base):
    def post(self, data):
        return self.client.post("/calendar/calls/schedule/", data=json.dumps(data), content_type="application/json")

    def wall(self, hours=24):
        return (timezone.now() + timedelta(hours=hours)).astimezone(LA).strftime("%Y-%m-%dT%H:%M")

    def test_options_know_the_leads_time_zone_and_who_calls(self):
        conv = self.conv("NY", assigned=self.agent)
        self.client.force_login(self.owner)
        data = self.client.get("/calendar/calls/options/", {"conversation": conv.id}).json()
        self.assertEqual(data["lead"]["tz"], "America/New_York")
        self.assertEqual(data["viewer"]["tz"], "America/Los_Angeles")
        self.assertEqual(data["default_member"], str(self.agent.public_id))
        self.assertEqual({m["name"] for m in data["members"]}, {"Olivia", "Aaron"})
        self.assertIsNone(data["existing"])

        self.client.force_login(self.agent)
        self.assertEqual(len(self.client.get("/calendar/calls/options/", {"conversation": conv.id}).json()["members"]), 1)

    def test_agent_books_their_own_call(self):
        conv = self.conv("CA", assigned=self.agent)
        self.client.force_login(self.agent)
        res = self.post({"conversation_id": conv.id, "start": self.wall(), "duration": 30, "reminder_minutes": 15,
                         "notes": "2 drivers", "member": str(self.owner.public_id)})
        self.assertEqual(res.status_code, 200, res.content)
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual((event.user, event.kind, event.source, event.reminder_minutes), (self.agent, "call", "manual", 15))
        self.assertEqual(event.end_at - event.start_at, timedelta(minutes=30))
        self.assertIn("Call booked by Aaron", self.call_notes(conv)[-1])
        self.assertEqual(self.sent, [])                 # no need to notify yourself

    def test_owner_books_for_an_agent_and_the_agent_is_told(self):
        conv = self.conv("CA")
        self.client.force_login(self.owner)
        self.post({"conversation_id": conv.id, "start": self.wall(), "member": str(self.agent.public_id)})
        event = CalendarEvent.objects.get(conversation=conv)
        self.assertEqual(event.user, self.agent)
        [booked] = self.reminders_sent("booked")
        self.assertEqual(booked["assigned_to_id"], self.agent.id)

    def test_changing_the_call_moves_it(self):
        conv = self.conv("CA", assigned=self.agent)
        self.client.force_login(self.agent)
        self.post({"conversation_id": conv.id, "start": self.wall(24)})
        event = CalendarEvent.objects.get(conversation=conv)
        existing = self.client.get("/calendar/calls/options/", {"conversation": conv.id}).json()["existing"]
        self.assertEqual(existing["event_id"], event.id)
        self.post({"conversation_id": conv.id, "start": self.wall(30), "event_id": event.id})
        self.assertEqual(CalendarEvent.objects.filter(conversation=conv).count(), 1)
        self.assertIn("Call moved", self.call_notes(conv)[-1])

    def test_bad_requests(self):
        conv = self.conv("CA")
        self.client.force_login(self.agent)
        self.assertEqual(self.post({"conversation_id": conv.id, "start": self.wall(-2)}).status_code, 400)
        self.assertEqual(self.post({"conversation_id": conv.id, "start": self.wall(), "reminder_minutes": 7}).status_code, 400)
        other_org = Organization.objects.create(name="Other", slug="other-calls")
        other_phone = PhoneNumber.objects.create(organization=other_org, e164="+16505550299", is_active=True)
        other = Conversation.objects.create(organization=other_org, phone_number=other_phone,
                                            contact=Contact.objects.create(organization=other_org, phone="+16505550300"))
        self.assertEqual(self.post({"conversation_id": other.id, "start": self.wall()}).status_code, 404)
        self.assertEqual(self.client.get("/calendar/calls/options/", {"conversation": other.id}).status_code, 404)

    @override_settings(MESSAGING_PROVIDER="fake")
    def test_chat_shows_the_booked_call(self):
        conv = self.conv("CA", assigned=self.agent)
        calls.book_call(contact=conv.contact, user=self.agent, conversation=conv,
                        start=timezone.now() + timedelta(days=1), source=CalendarEvent.Source.AI)
        self.client.force_login(self.agent)
        res = self.client.get(f"/inbox/{conv.id}/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertContains(res, "Call booked:")
        self.assertContains(res, "with Aaron")
        self.assertContains(res, "by Gemma")
        self.assertContains(res, "Schedule call")


# ---------------------------------------------------------------------------
# Replies found wrong in the live run of every scenario
# ---------------------------------------------------------------------------

@override_settings(MESSAGING_PROVIDER="fake", AI_CLIENT="fake")
class ReplySafetyTests(Base):
    def setUp(self):
        super().setUp()
        AIAssistantConfig.objects.create(
            organization=self.org, business_name="Pacific Test", mode=AIAssistantConfig.Mode.AUTO,
            qualification_questions=[{"key": "service_type", "question": "What can we help you with today?",
                                      "required": True}])

    def ask(self, text, *replies):
        conv = self.conv("CA")
        msg = Message.objects.create(organization=self.org, conversation=conv, direction=Message.Direction.INBOUND,
                                     sender_type=Message.SenderType.CUSTOMER, body=text, status=Message.Status.RECEIVED)
        for r in replies:
            FakeAIClient.push_response({"reply": r, "intent": "inquiry", "collected": {}, "needs_human": False,
                                        "handoff_reason": None, "checks": {}})
        process_ai_job(AIJob.objects.create(conversation=conv, trigger_message=msg, status=AIJob.Status.PENDING,
                                            run_after=timezone.now()))
        conv.refresh_from_db()
        return conv, Message.objects.filter(conversation=conv, sender_type=Message.SenderType.AI).order_by(
            "-created_at").values_list("body", flat=True).first()

    def test_are_you_a_bot_keeps_the_answer(self):
        # Live: the answer ended with "How can I help you today?" and was replaced by the next question
        conv, reply = self.ask("are you a bot?", "I'm an AI assistant helping the team, but a real teammate is "
                                                 "right here if you need! How can I help you today?")
        self.assertEqual(reply, "I'm an AI assistant helping the team, but a real teammate is right here if you need!")

    def test_a_blocked_guarantee_reply_is_rewritten_not_left_silent(self):
        conv, reply = self.ask("can you guarantee my rate will go down?",
                               "We can't guarantee that, but an agent can review your options!",
                               "Rates depend on your details, but a licensed agent can go over your options with you!")
        self.assertEqual(reply, "Rates depend on your details, but a licensed agent can go over your options with you!")
        self.assertEqual(conv.status, Conversation.Status.OPEN)

    def test_if_the_rewrite_is_blocked_too_a_safe_reply_goes_out_and_an_agent_takes_over(self):
        conv, reply = self.ask("can you guarantee my rate will go down?",
                               "We can't guarantee that!", "No guarantee, sorry!")
        self.assertEqual(reply, "Good question! A licensed agent will follow up with you on that shortly.")
        self.assertEqual(conv.status, Conversation.Status.NEEDS_HUMAN)
        self.assertIn("Content safety check failed", conv.handoff_reason)


# ---------------------------------------------------------------------------
# Call hours set by the team (inside the legal 8 AM-9 PM window)
# ---------------------------------------------------------------------------

@override_settings(MESSAGING_PROVIDER="fake", AI_CLIENT="fake")
class CallHoursTests(Base):
    def setUp(self):
        super().setUp()
        AIAssistantConfig.objects.create(organization=self.org, business_name="Pacific Test",
                                         mode=AIAssistantConfig.Mode.AUTO)
        self.org.call_start, self.org.call_end = time_cls(9, 0), time_cls(17, 0)
        self.org.save()

    def test_window_and_next_slot_follow_the_team_hours(self):
        self.assertTrue(calls.team_hours_text(self.org).startswith("9 AM-5 PM"))       # e.g. "9 AM-5 PM PST, every day"
        late = datetime(2026, 11, 24, 16, 45, tzinfo=LA)                          # 4:45 PM: too late today
        self.assertEqual(calls.next_call_slot(late, "America/Los_Angeles", self.org),
                         datetime(2026, 11, 25, 9, 0, tzinfo=LA))
        early = datetime(2026, 11, 24, 6, 0, tzinfo=LA)
        self.assertEqual(calls.next_call_slot(early, "America/Los_Angeles", self.org),
                         datetime(2026, 11, 24, 9, 0, tzinfo=LA))
        self.assertTrue(calls.in_calling_hours(datetime(2026, 11, 24, 16, 30, tzinfo=LA), "America/Los_Angeles", self.org))
        self.assertFalse(calls.in_calling_hours(datetime(2026, 11, 24, 18, 0, tzinfo=LA), "America/Los_Angeles", self.org))

    def test_calls_never_go_outside_the_leads_legal_window(self):
        # The team works 6 AM-11 PM, but a California lead can still only be called 8 AM-9 PM their time
        self.org.call_start, self.org.call_end = time_cls(6, 0), time_cls(23, 0)
        day = datetime(2026, 11, 24).date()
        self.assertEqual(calls.lead_window(self.org, "America/Los_Angeles", day), (time_cls(8, 0), time_cls(21, 0)))

    def test_team_hours_are_the_teams_own_time_not_the_leads(self):
        # A California team working 9 AM-5 PM Pacific = 12 PM-8 PM for a New York lead
        day = datetime(2026, 11, 24).date()
        self.assertEqual(calls.lead_window(self.org, "America/New_York", day), (time_cls(12, 0), time_cls(20, 0)))
        ny = ZoneInfo("America/New_York")
        self.assertTrue(calls.can_call_at(datetime(2026, 11, 24, 19, 0, tzinfo=ny), "America/New_York", self.org))
        self.assertFalse(calls.can_call_at(datetime(2026, 11, 24, 10, 0, tzinfo=ny), "America/New_York", self.org))
        early = datetime(2026, 11, 24, 7, 0, tzinfo=ny)
        self.assertEqual(calls.next_call_slot(early, "America/New_York", self.org), datetime(2026, 11, 24, 12, 0, tzinfo=ny))
        self.assertEqual(calls.lead_hours_text(self.org, "America/New_York", day), "12 PM-8 PM, every day")

    def test_a_time_after_the_team_hours_is_declined(self):
        conv = self.conv("CA")
        day = (timezone.now().astimezone(LA) + timedelta(days=1)).date()
        Message.objects.create(organization=self.org, conversation=conv, direction=Message.Direction.OUTBOUND,
                               sender_type=Message.SenderType.AI, status=Message.Status.DELIVERED,
                               body="Want a licensed agent to call you with options? What time works?")
        msg = Message.objects.create(organization=self.org, conversation=conv, direction=Message.Direction.INBOUND,
                                     sender_type=Message.SenderType.CUSTOMER, body="tomorrow at 6pm",
                                     status=Message.Status.RECEIVED)
        FakeAIClient.push_response({"reply": "Sure, 6pm works!", "intent": "answer", "collected": {},
                                    "needs_human": True, "handoff_reason": "Wants a call - best time: 6pm",
                                    "checks": {}})
        FakeAIClient.push_response({"wants_call": True, "date": day.isoformat(), "time": "18:00", "part_of_day": "",
                                    "asap": False, "their_words": "tomorrow at 6pm", "day_said": True,
                                    "am_pm_said": True})
        process_ai_job(AIJob.objects.create(conversation=conv, trigger_message=msg, status=AIJob.Status.PENDING,
                                            run_after=timezone.now()))
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        note = Message.objects.filter(conversation=conv, body__contains="📅").last().body
        self.assertIn("9 AM-5 PM", note)

    def test_settings_save_call_days_and_hours(self):
        self.client.force_login(self.owner)
        base = {"name": "Pacific Test", "address_state": "CA", "call_days_present": "1", "call_days": ["0", "1", "2"]}
        self.client.post("/settings/profile/", {**base, "call_start": "10:00", "call_end": "18:30"})
        self.org.refresh_from_db()
        self.assertEqual((self.org.call_days, self.org.call_start, self.org.call_end),
                         ([0, 1, 2], time_cls(10, 0), time_cls(18, 30)))
        self.client.post("/settings/profile/", {**base, "call_start": "18:00", "call_end": "09:00"})   # backwards
        self.org.refresh_from_db()
        self.assertEqual((self.org.call_start, self.org.call_end), (time_cls(10, 0), time_cls(18, 30)))
        page = self.client.get("/settings/profile/").content.decode()
        self.assertIn('value="10:00" selected', page)
        self.assertIn('value="18:30" selected', page)


class SettingsAreForOwnersAndAdminsTests(Base):
    """The whole Settings tab: agents can look, only owners and admins can change anything."""

    def test_agents_cannot_change_any_setting(self):
        self.client.force_login(self.agent)
        before = (self.org.name, self.org.call_days, self.org.call_start, self.org.call_end, self.org.timezone,
                  self.org.lead_routing_strategy)
        self.client.post("/settings/profile/", {"name": "Hacked", "timezone": "America/New_York", "call_days_present": "1",
                                                "call_days": ["5"], "call_start": "06:00", "call_end": "07:00"})
        self.client.post("/settings/routing/", {"lead_routing_strategy": "round_robin"})
        self.org.refresh_from_db()
        self.assertEqual((self.org.name, self.org.call_days, self.org.call_start, self.org.call_end, self.org.timezone,
                          self.org.lead_routing_strategy), before)
        r = self.client.post("/settings/team/invite/", {"email": "x@example.com", "role": "admin"})
        self.assertIn(r.status_code, (302, 403))
        r = self.client.post(f"/settings/team/members/{self.owner.id}/role/", {"role": "agent"})
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.role, User.Role.OWNER)

    def test_agents_see_settings_read_only(self):
        self.client.force_login(self.agent)
        for page in ("/settings/profile/", "/settings/team/", "/settings/phone-numbers/", "/settings/sounds/"):
            html = self.client.get(page).content.decode()
            self.assertIn("read-only access", html, page)
        profile = self.client.get("/settings/profile/").content.decode()
        self.assertIn('name="call_start" disabled', profile)
        self.assertIn('name="call_end" disabled', profile)

    def test_admins_can_change_settings(self):
        admin = User.objects.create_user(email="admin@calls.test", password="pw-123456789", organization=self.org,
                                         role=User.Role.ADMIN, first_name="Ada")
        self.client.force_login(admin)
        self.client.post("/settings/profile/", {"name": "Pacific Test", "address_state": "CA", "call_days_present": "1",
                                                "call_days": ["0", "1"], "call_start": "09:00", "call_end": "17:00"})
        self.org.refresh_from_db()
        self.assertEqual((self.org.call_days, self.org.call_start, self.org.call_end),
                         ([0, 1], time_cls(9, 0), time_cls(17, 0)))


class GemmaKnowsTheCallHoursTests(Base):
    def test_prompt_states_the_call_hours_in_the_leads_time(self):
        from messaging.ai.prompt import build_system_prompt
        config = AIAssistantConfig.objects.create(organization=self.org, business_name="Pacific Test",
                                                  knowledge="Hours: Monday-Friday 9:00 AM - 6:00 PM.")
        self.org.call_days, self.org.call_start, self.org.call_end = [0, 1, 2, 3, 4], time_cls(10, 0), time_cls(18, 0)
        self.org.save()
        ca = build_system_prompt(config, self.conv("CA"))
        self.assertIn("- Call hours: our agents can call this customer 10 AM-6 PM, Monday-Friday", ca)
        ny = build_system_prompt(config, self.conv("NY"))
        self.assertIn("- Call hours: our agents can call this customer 1 PM-9 PM, Monday-Friday", ny)   # 10-6 Pacific
