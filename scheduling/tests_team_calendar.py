"""Team call calendar: Gemma's first call with a new lead goes to the team to claim, unclaimed calls are assigned
automatically, a limit on calls at the same time, and closed days (federal holidays and dates the owner sets)."""
from datetime import date, datetime, timedelta
from datetime import time as time_cls

from django.test import override_settings
from django.utils import timezone

from core.models import User
from messaging.ai.fake_client import FakeAIClient
from messaging.ai.pipeline import process_ai_job
from messaging.ai.prompt import build_system_prompt
from messaging.models import AIAssistantConfig, AIJob, Conversation, Message
from scheduling import calls, holidays
from scheduling.models import CalendarEvent, ClosedDay
from scheduling.reminders import send_due_reminders
from scheduling.tests_calls import LA, Base


@override_settings(MESSAGING_PROVIDER="fake", AI_CLIENT="fake")
class TeamCallTestCase(Base):
    def setUp(self):
        super().setUp()
        self.config = AIAssistantConfig.objects.create(organization=self.org, business_name="Pacific Test",
                                                       mode=AIAssistantConfig.Mode.AUTO)
        self.org.call_pool_enabled = True
        self.org.save()
        self.agent2 = User.objects.create_user(email="agent2@calls.test", password="pw-123456789",
                                               organization=self.org, role=User.Role.AGENT, first_name="Bea")

    def tomorrow_at(self, hour, minute=0):
        day = (timezone.now().astimezone(LA) + timedelta(days=1)).date()
        return datetime.combine(day, time_cls(hour, minute), tzinfo=LA)

    def say(self, conv, text, reply, read=None, needs_human=True, reason="Wants a call"):
        Message.objects.create(organization=self.org, conversation=conv, direction=Message.Direction.OUTBOUND,
                               sender_type=Message.SenderType.AI, status=Message.Status.DELIVERED,
                               body="Want a licensed agent to call you with options? What time works?")
        msg = Message.objects.create(organization=self.org, conversation=conv, direction=Message.Direction.INBOUND,
                                     sender_type=Message.SenderType.CUSTOMER, body=text,
                                     status=Message.Status.RECEIVED)
        FakeAIClient.push_response({"reply": reply, "intent": "answer", "collected": {}, "needs_human": needs_human,
                                    "handoff_reason": reason if needs_human else None, "checks": {}})
        if read is not None:
            FakeAIClient.push_response({"wants_call": True, "date": "", "time": "", "part_of_day": "", "asap": False,
                                        "their_words": "", "day_said": True, "am_pm_said": True, **read})
        process_ai_job(AIJob.objects.create(conversation=conv, trigger_message=msg, status=AIJob.Status.PENDING,
                                            run_after=timezone.now()))
        conv.refresh_from_db()
        return Message.objects.filter(conversation=conv, sender_type=Message.SenderType.AI).order_by(
            "-created_at").values_list("body", flat=True).first()

    def book_tomorrow_10(self, conv):
        at = self.tomorrow_at(10)
        self.say(conv, "call me tomorrow at 10am", "Great!",
                 read={"date": at.date().isoformat(), "time": "10:00", "their_words": "tomorrow at 10am"})
        return CalendarEvent.objects.get(conversation=conv)

    def fresh_org(self):
        from core.models import Organization
        return Organization.objects.get(pk=self.org.pk)       # closed days are cached per org object

    def pool_alerts(self):
        return [(uid, p["reminder"]) for uid, p in self.sent
                if p.get("event") == "call_reminder" and p["reminder"]["stage"] == "pool"]


class TeamCallPoolTests(TeamCallTestCase):
    def test_a_new_leads_call_goes_to_the_team_and_everyone_available_hears(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        self.assertIsNone(event.user)                                   # nobody has it yet
        self.assertIsNotNone(event.pooled_at)
        self.assertEqual(event.start_at, self.tomorrow_at(10))
        self.assertIsNone(conv.assigned_to)                             # the lead waits for whoever claims it
        self.assertEqual(conv.status, Conversation.Status.NEEDS_HUMAN)
        self.assertIn("first agent to claim it gets the lead", event.notes)
        self.assertIn("with the team (not claimed yet)", self.call_notes(conv)[-1])
        told = {uid for uid, _ in self.pool_alerts()}
        self.assertEqual(told, {self.agent.id, self.agent2.id})        # the owner isn't marked available
        [(_, alert)] = [a for a in self.pool_alerts() if a[0] == self.agent.id]
        self.assertEqual(alert["url"], f"/calendar/?claim={event.id}")
        self.assertIn("first to claim it gets the lead", alert["body"])

    def test_a_lead_with_an_agent_goes_straight_to_them(self):
        conv = self.conv("CA", assigned=self.agent2)
        event = self.book_tomorrow_10(conv)
        self.assertEqual(event.user, self.agent2)
        self.assertEqual(self.pool_alerts(), [])

    def test_with_the_team_calendar_off_the_lead_routing_picks_the_caller(self):
        self.org.call_pool_enabled = False
        self.org.save()
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        self.assertIsNotNone(event.user)
        self.assertEqual(conv.assigned_to, event.user)

    def test_claiming_a_call_gives_the_lead_too_and_only_once(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        self.client.force_login(self.agent2)
        r = self.client.post(f"/calendar/calls/{event.id}/claim/")
        self.assertEqual(r.json()["status"], "success")
        event.refresh_from_db()
        conv.refresh_from_db()
        self.assertEqual((event.user, conv.assigned_to, event.auto_assigned), (self.agent2, self.agent2, False))
        self.assertIsNotNone(event.claimed_at)
        self.assertIn("Claimed by Bea", self.call_notes(conv)[-1])
        # the next person is told it's gone
        self.client.force_login(self.agent)
        r = self.client.post(f"/calendar/calls/{event.id}/claim/")
        self.assertEqual(r.status_code, 409)
        self.assertIn("Bea", r.json()["message"])

    def test_the_unclaimed_list_and_every_calendar_show_the_call(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        self.client.force_login(self.agent)
        rows = self.client.get("/calendar/calls/unclaimed/").json()["calls"]
        self.assertEqual([r["id"] for r in rows], [event.id])
        self.assertIn("assigned automatically", self.client.get("/calendar/").content.decode())
        start, end = (event.start_at - timedelta(days=1)).isoformat(), (event.start_at + timedelta(days=1)).isoformat()
        feed = self.client.get("/calendar/events/", {"start": start, "end": end}).json()["events"]
        [shown] = [e for e in feed if e.get("id") == event.id]
        self.assertTrue(shown["extendedProps"]["can_claim"])
        self.assertEqual(shown["extendedProps"]["owner_name"], "Unclaimed")
        self.assertFalse(shown["editable"])                              # agents claim it, they don't edit it

    def test_an_owner_can_give_a_team_call_to_someone(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        self.client.force_login(self.owner)
        r = self.client.post(f"/calendar/calls/{event.id}/claim/", {"member": str(self.agent.public_id)})
        self.assertEqual(r.json()["status"], "success")
        event.refresh_from_db()
        self.assertEqual(event.user, self.agent)
        self.assertTrue(any(uid == self.agent.id and p["reminder"]["stage"] == "booked"
                            for uid, p in self.sent if p.get("event") == "call_reminder"))

    def test_unclaimed_calls_are_assigned_automatically_before_the_call(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        # 31 minutes before: still waiting for someone to claim it
        send_due_reminders(now=event.start_at - timedelta(minutes=31))
        event.refresh_from_db()
        self.assertIsNone(event.user)
        # 30 minutes before (the default): the lead routing picks an agent
        send_due_reminders(now=event.start_at - timedelta(minutes=30))
        event.refresh_from_db()
        conv.refresh_from_db()
        self.assertIn(event.user, (self.agent, self.agent2))
        self.assertTrue(event.auto_assigned)
        self.assertEqual(conv.assigned_to, event.user)
        self.assertIn("assigned automatically", self.call_notes(conv)[-1])
        self.assertTrue(any(uid == event.user_id and p["reminder"]["stage"] == "assigned"
                            for uid, p in self.sent if p.get("event") == "call_reminder"))

    def test_the_owner_picks_when_unclaimed_calls_are_assigned(self):
        self.org.call_claim_minutes = 60
        self.org.save()
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        send_due_reminders(now=event.start_at - timedelta(minutes=61))
        event.refresh_from_db()
        self.assertIsNone(event.user)
        send_due_reminders(now=event.start_at - timedelta(minutes=60))
        event.refresh_from_db()
        self.assertIsNotNone(event.user)

    def test_a_call_booked_close_to_its_start_still_gets_a_moment_to_be_claimed(self):
        conv = self.conv("CA")
        start = timezone.now() + timedelta(minutes=20)                  # already inside the 30 minutes
        event, _ = calls.book_call(contact=conv.contact, user=None, start=start, conversation=conv)
        send_due_reminders(now=timezone.now())
        event.refresh_from_db()
        self.assertIsNone(event.user)
        send_due_reminders(now=timezone.now() + calls.CLAIM_GRACE)
        event.refresh_from_db()
        self.assertIsNotNone(event.user)


class CallsAtTheSameTimeTests(TeamCallTestCase):
    def test_the_default_limit_is_the_agents_available_for_leads(self):
        self.assertEqual(calls.slot_limit(self.org), 2)                 # agent + agent2 (the owner is away)
        User.objects.filter(organization=self.org).update(is_available_for_leads=False)
        self.assertEqual(calls.slot_limit(self.org), 3)                 # nobody available: the whole team
        self.org.max_calls_per_slot = 5
        self.assertEqual(calls.slot_limit(self.org), 5)

    def test_a_full_time_gets_the_next_open_time_suggested(self):
        self.org.max_calls_per_slot = 1
        self.org.save()
        other = self.conv("CA", assigned=self.agent)
        calls.book_call(contact=other.contact, user=self.agent, start=self.tomorrow_at(10), conversation=other)
        conv = self.conv("CA")
        reply = self.say(conv, "call me tomorrow at 10am", "Great!",
                         read={"date": self.tomorrow_at(10).date().isoformat(), "time": "10:00",
                               "their_words": "tomorrow at 10am"})
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertIn("10:15 AM", reply)                                # the next quarter hour that's free
        self.assertIn("fully booked", reply)
        self.assertIn("every agent already has a call then", self.call_notes(conv)[-1])
        # with room for two, the same request is booked
        self.org.max_calls_per_slot = 2
        self.org.save()
        conv2 = self.conv("CA")
        self.book_tomorrow_10(conv2)

    def test_the_next_open_slot_skips_full_times(self):
        self.org.max_calls_per_slot = 1
        self.org.save()
        other = self.conv("CA", assigned=self.agent)
        now = timezone.now()
        first = calls.next_call_slot(now, "America/Los_Angeles", self.org)
        calls.book_call(contact=other.contact, user=self.agent, start=first, conversation=other)
        self.assertEqual(calls.next_call_slot(now, "America/Los_Angeles", self.org), first + timedelta(minutes=15))


class ClosedDayTests(TeamCallTestCase):
    def test_federal_holiday_dates(self):
        d = holidays.holiday_dates(2026)
        self.assertEqual((d["thanksgiving"], d["memorial"], d["labor"]),
                         (date(2026, 11, 26), date(2026, 5, 25), date(2026, 9, 7)))
        self.assertEqual(holidays.holiday_dates(2027)["christmas"], date(2027, 12, 24))   # Sat -> Fri
        self.assertEqual(holidays.holiday_on(date(2027, 12, 31)), "new_year")              # Jan 1 2028 is a Sat
        self.assertEqual(holidays.DEFAULT_CLOSED,
                         ["new_year", "memorial", "independence", "labor", "thanksgiving", "christmas"])

    def test_default_holidays_are_closed_and_others_open(self):
        self.org.closed_holidays = list(holidays.DEFAULT_CLOSED)
        self.org.save()
        thanksgiving = datetime(2027, 11, 25, 10, 0, tzinfo=LA)
        columbus = datetime(2027, 10, 11, 10, 0, tzinfo=LA)
        self.assertFalse(calls.can_call_at(thanksgiving, "America/Los_Angeles", self.org))
        self.assertEqual(calls.closed_reason_at(thanksgiving, self.org), "Thanksgiving")
        self.assertTrue(calls.can_call_at(columbus, "America/Los_Angeles", self.org))
        self.assertEqual(calls.callable_instead(thanksgiving, "America/Los_Angeles", self.org,
                                                now=datetime(2027, 11, 20, tzinfo=LA)),
                         datetime(2027, 11, 26, 10, 0, tzinfo=LA))

    def test_a_holiday_can_be_opened_for_one_day_and_other_days_closed(self):
        self.org.closed_holidays = list(holidays.DEFAULT_CLOSED)
        self.org.save()
        self.client.force_login(self.owner)
        thanksgiving = "2027-11-25"
        r = self.client.post("/calendar/closed-days/", {"date": thanksgiving, "action": "open"})
        self.assertEqual(r.json()["status"], "success")
        self.assertFalse(holidays.is_closed(self.fresh_org(), date(2027, 11, 25)))
        row = next(d for d in r.json()["days"] if d["date"] == thanksgiving)
        self.assertEqual((row["closed"], row["kind"]), (False, "reopened"))
        self.client.post("/calendar/closed-days/", {"date": thanksgiving, "action": "reset"})
        self.assertTrue(holidays.is_closed(self.fresh_org(), date(2027, 11, 25)))
        # an ordinary day, closed with a reason
        self.client.post("/calendar/closed-days/", {"date": "2027-03-03", "action": "close", "name": "Office party"})
        self.assertEqual(holidays.closed_reason(self.fresh_org(), date(2027, 3, 3)), "Office party")
        self.client.post("/calendar/closed-days/", {"date": "2027-03-03", "action": "open"})
        self.assertFalse(ClosedDay.objects.filter(organization=self.org, date=date(2027, 3, 3)).exists())

    def test_only_owners_and_admins_change_closed_days(self):
        self.client.force_login(self.agent)
        r = self.client.post("/calendar/closed-days/", {"date": "2027-03-03", "action": "close"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["message"], "Only owners and admins can change closed days.")
        self.assertEqual(self.client.get("/calendar/closed-days/").status_code, 200)     # everyone can see them

    def test_gemma_names_the_closed_day_and_suggests_the_next_open_one(self):
        tomorrow = self.tomorrow_at(10)
        ClosedDay.objects.create(organization=self.org, date=tomorrow.date(), name="Office party")
        conv = self.conv("CA")
        reply = self.say(conv, "call me tomorrow at 10am", "Sure!",
                         read={"date": tomorrow.date().isoformat(), "time": "10:00", "their_words": "tomorrow at 10am"})
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertIn("our office is closed", reply)
        self.assertIn("Office party", reply)
        self.assertIn(f"{(tomorrow + timedelta(days=1)):%a, %b} {(tomorrow + timedelta(days=1)).day}", reply)
        self.assertIn("Office party", build_system_prompt(self.config, conv))            # Gemma knows in advance

    def test_closed_days_show_on_the_calendar(self):
        ClosedDay.objects.create(organization=self.org, date=self.tomorrow_at(10).date(), name="Office party")
        self.client.force_login(self.agent)
        start = timezone.now().isoformat()
        end = (timezone.now() + timedelta(days=3)).isoformat()
        feed = self.client.get("/calendar/events/", {"start": start, "end": end}).json()["events"]
        [shaded] = [e for e in feed if e.get("display") == "background"]
        self.assertEqual(shaded["title"], "Closed · Office party")


class TeamCalendarSettingsTests(TeamCallTestCase):
    def post_profile(self, user, **extra):
        self.client.force_login(user)
        data = {"name": "Pacific Test", "address_state": "CA", "team_calendar_present": "1",
                "call_claim_minutes": "30", "slot_limit_mode": "agents", **extra}
        return self.client.post("/settings/profile/", data)

    def test_owner_sets_the_team_calendar(self):
        self.post_profile(self.owner, call_pool_enabled="on", call_claim_minutes="60", slot_limit_mode="custom",
                          max_calls_per_slot="3", closed_holidays=["thanksgiving", "veterans"])
        self.org.refresh_from_db()
        self.assertEqual((self.org.call_pool_enabled, self.org.call_claim_minutes, self.org.max_calls_per_slot),
                         (True, 60, 3))
        self.assertEqual(sorted(self.org.closed_holidays), ["thanksgiving", "veterans"])
        # back to "the number of available agents", team calendar off, no holidays
        self.post_profile(self.owner)
        self.org.refresh_from_db()
        self.assertEqual((self.org.call_pool_enabled, self.org.max_calls_per_slot, self.org.closed_holidays),
                         (False, None, []))

    def test_bad_values_are_refused(self):
        self.post_profile(self.owner, call_pool_enabled="on", call_claim_minutes="7")
        self.org.refresh_from_db()
        self.assertEqual(self.org.call_claim_minutes, 30)
        self.post_profile(self.owner, call_pool_enabled="on", slot_limit_mode="custom", max_calls_per_slot="0")
        self.org.refresh_from_db()
        self.assertIsNone(self.org.max_calls_per_slot)

    def test_agents_cannot_change_it(self):
        self.post_profile(self.agent, call_claim_minutes="60")
        self.org.refresh_from_db()
        self.assertEqual(self.org.call_claim_minutes, 30)

    def test_the_page_shows_the_card_with_holidays(self):
        self.client.force_login(self.owner)
        html = self.client.get("/settings/profile/").content.decode()
        self.assertIn("Team call calendar", html)
        self.assertIn("Thanksgiving", html)
        self.assertIn("Calls at the same time", html)
