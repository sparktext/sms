"""Team call calendar, every condition: who hears about a call, moving / cancelling a team call, claiming from every
door (Claim button, editor, Schedule call, owner handing it out), 4 agents claiming at once, auto-assign fallbacks,
calls at the same time, and closed days across time zones."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from threading import Barrier

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from contacts.models import Contact
from core.models import Organization, User
from messaging.ai.prompt import build_system_prompt
from messaging.models import Conversation, Message, PhoneNumber
from scheduling import calls, holidays
from scheduling.models import CalendarEvent, ClosedDay
from scheduling.reminders import send_due_reminders
from scheduling.tests_calls import LA, NY
from scheduling.tests_team_calendar import TeamCallTestCase


def alerts(test, stage=None):
    return [(uid, p["reminder"]) for uid, p in test.sent
            if p.get("event") == "call_reminder" and (stage is None or p["reminder"]["stage"] == stage)]


class WhoHearsAboutTeamCallsTests(TeamCallTestCase):
    def test_when_nobody_is_available_the_whole_team_hears(self):
        User.objects.filter(organization=self.org).update(is_available_for_leads=False)
        self.book_tomorrow_10(self.conv("CA"))
        self.assertEqual({uid for uid, _ in alerts(self, "pool")}, {self.owner.id, self.agent.id, self.agent2.id})

    def test_inactive_people_never_hear_or_get_calls(self):
        self.agent2.is_active = False
        self.agent2.save()
        self.book_tomorrow_10(self.conv("CA"))
        self.assertNotIn(self.agent2.id, {uid for uid, _ in alerts(self, "pool")})

    def test_the_bell_and_prompt_and_chat_banner_say_the_team_has_it(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        self.assertIn("one of our agents", build_system_prompt(self.config, conv))
        self.assertIn("with the team (not claimed yet)", calls.upcoming_call_banner(conv, self.agent)["text"])
        self.assertTrue(calls.booking_summary(event, "America/Los_Angeles").endswith("with the team (not claimed yet)"))


class MovingAndCancellingTeamCallsTests(TeamCallTestCase):
    def test_the_lead_moves_an_unclaimed_call_and_it_stays_with_the_team(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        self.sent.clear()
        self.say(conv, "can we do 3pm instead", "Sure!",
                 read={"date": self.tomorrow_at(15).date().isoformat(), "time": "15:00", "their_words": "3pm"})
        event.refresh_from_db()
        conv.refresh_from_db()
        self.assertEqual((event.start_at, event.user, conv.assigned_to), (self.tomorrow_at(15), None, None))
        self.assertEqual(CalendarEvent.objects.filter(conversation=conv).count(), 1)
        self.assertTrue(alerts(self, "pool"))                           # the team hears about the new time
        self.assertIn("Call moved by Gemma", self.call_notes(conv)[-1])

    def test_a_claimed_call_that_moves_stays_with_the_claimer(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        calls.claim_call(event, self.agent2)
        self.say(conv, "can we do 3pm instead", "Sure!",
                 read={"date": self.tomorrow_at(15).date().isoformat(), "time": "15:00", "their_words": "3pm"})
        event.refresh_from_db()
        self.assertEqual((event.start_at, event.user), (self.tomorrow_at(15), self.agent2))

    def test_cancelling_an_unclaimed_call_tells_the_team_its_gone(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        self.sent.clear()
        self.say(conv, "please cancel the call", "No problem!", needs_human=False)
        event.refresh_from_db()
        self.assertEqual(event.status, CalendarEvent.Status.CANCELLED)
        self.assertEqual(alerts(self, "pool"), [])                      # never "a new call to claim"
        self.assertEqual({uid for uid, _ in alerts(self, "cancelled")}, {self.agent.id, self.agent2.id})
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get("/calendar/calls/unclaimed/").json()["calls"], [])
        r = self.client.post(f"/calendar/calls/{event.id}/claim/")
        self.assertEqual(r.json()["message"], "That call isn't scheduled anymore.")


class EveryWayToClaimTests(TeamCallTestCase):
    def test_schedule_call_on_a_team_call_claims_it_instead_of_adding_another(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        self.client.force_login(self.agent)
        options = self.client.get("/calendar/calls/options/", {"conversation_id": conv.id}).json()
        self.assertEqual(options["existing"]["event_id"], event.id)      # the agent sees the team call
        self.assertEqual(options["existing"]["member_name"], "Unclaimed (team call)")
        r = self.client.post("/calendar/calls/schedule/", {
            "conversation_id": conv.id, "event_id": event.id,
            "start": self.tomorrow_at(11).astimezone(LA).strftime("%Y-%m-%dT%H:%M"), "duration": 15})
        self.assertEqual(r.json()["status"], "success", r.json())
        event.refresh_from_db()
        conv.refresh_from_db()
        self.assertEqual(CalendarEvent.objects.filter(conversation=conv).count(), 1)
        self.assertEqual((event.user, conv.assigned_to, event.start_at), (self.agent, self.agent, self.tomorrow_at(11)))
        self.assertIsNotNone(event.claimed_at)

    def test_owner_moves_a_call_back_to_the_team_and_gives_it_out_in_the_editor(self):
        conv = self.conv("CA", assigned=self.agent)
        event, _ = calls.book_call(contact=conv.contact, user=self.agent, start=self.tomorrow_at(10), conversation=conv)
        self.client.force_login(self.owner)
        self.client.post(f"/calendar/events/{event.id}/update/", {"member": "pool"})
        event.refresh_from_db()
        self.assertIsNone(event.user)
        self.assertIsNotNone(event.pooled_at)
        self.client.post(f"/calendar/events/{event.id}/update/", {"member": str(self.agent2.public_id)})
        event.refresh_from_db()
        self.assertEqual(event.user, self.agent2)
        # an owner can also create a team call from scratch
        r = self.client.post("/calendar/events/create/", {"member": "pool", "kind": "call", "title": "Team call",
                                                           "start": "2027-01-05T10:00", "end": "2027-01-05T10:15"})
        self.assertTrue(r.json()["event"]["extendedProps"]["unclaimed"])

    def test_agents_cannot_edit_or_delete_a_team_call_or_put_it_in_the_pool(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        self.client.force_login(self.agent)
        self.assertEqual(self.client.post(f"/calendar/events/{event.id}/update/", {"title": "x"}).status_code, 404)
        self.assertEqual(self.client.post(f"/calendar/events/{event.id}/delete/").status_code, 404)
        r = self.client.post("/calendar/events/create/", {"member": "pool", "kind": "call", "title": "Mine",
                                                           "start": "2027-01-05T10:00", "end": "2027-01-05T10:15"})
        self.assertFalse(r.json()["event"]["extendedProps"]["unclaimed"])    # agents only add to their own
        r = self.client.post(f"/calendar/calls/{event.id}/claim/", {"member": str(self.agent2.public_id)})
        event.refresh_from_db()
        self.assertEqual(event.user, self.agent)                         # "member" is ignored for agents

    def test_another_workspace_cannot_see_or_claim_the_call(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        other_org = Organization.objects.create(name="Other", slug="other-org")
        stranger = User.objects.create_user(email="x@other.test", password="pw-123456789", organization=other_org,
                                            role=User.Role.OWNER)
        self.client.force_login(stranger)
        self.assertEqual(self.client.post(f"/calendar/calls/{event.id}/claim/").status_code, 404)
        self.assertEqual(self.client.get("/calendar/calls/unclaimed/").json()["calls"], [])


class AutoAssignFallbackTests(TeamCallTestCase):
    def due(self, event):
        send_due_reminders(now=event.start_at - timedelta(minutes=30))
        event.refresh_from_db()
        return event

    def test_with_manual_routing_it_goes_to_the_owner(self):
        self.org.lead_routing_strategy = Organization.LeadRoutingStrategy.DISABLED
        self.org.save()
        event = self.due(self.book_tomorrow_10(self.conv("CA")))
        self.assertEqual(event.user, self.owner)
        self.assertTrue(event.auto_assigned)

    def test_when_everyone_is_away_it_goes_to_the_owner(self):
        event = self.book_tomorrow_10(self.conv("CA"))
        User.objects.filter(organization=self.org).update(is_available_for_leads=False)
        self.assertEqual(self.due(event).user, self.owner)

    def test_a_lead_someone_took_meanwhile_brings_the_call_to_them(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        Conversation.objects.filter(pk=conv.pk).update(assigned_to=self.agent2)
        self.assertEqual(self.due(event).user, self.agent2)

    def test_reminders_only_start_once_someone_has_the_call(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        self.sent.clear()
        send_due_reminders(now=event.start_at - timedelta(minutes=31))
        self.assertEqual(alerts(self, "soon"), [])                       # nobody to remind yet
        send_due_reminders(now=event.start_at - timedelta(minutes=10))   # assigned (30 min) and the 10-min heads-up
        event.refresh_from_db()
        self.assertEqual([uid for uid, _ in alerts(self, "soon")], [event.user_id])

    def test_cancelled_or_finished_calls_are_never_assigned(self):
        conv = self.conv("CA")
        event = self.book_tomorrow_10(conv)
        CalendarEvent.objects.filter(pk=event.pk).update(status=CalendarEvent.Status.CANCELLED)
        self.assertIsNone(self.due(event).user)


class CallsAtTheSameTimeConditionsTests(TeamCallTestCase):
    def setUp(self):
        super().setUp()
        self.org.max_calls_per_slot = 1
        self.org.save()
        self.other = self.conv("CA", assigned=self.agent)

    def test_overlap_cancelled_and_other_workspaces(self):
        ten = self.tomorrow_at(10)
        event, _ = calls.book_call(contact=self.other.contact, user=self.agent, start=ten, conversation=self.other)
        self.assertTrue(calls.slot_is_full(self.org, ten + timedelta(minutes=10)))      # overlaps 10:00-10:15
        self.assertFalse(calls.slot_is_full(self.org, ten + timedelta(minutes=15)))     # starts as it ends
        self.assertFalse(calls.slot_is_full(self.org, ten, exclude=event))              # moving that same call
        CalendarEvent.objects.filter(pk=event.pk).update(status=CalendarEvent.Status.CANCELLED)
        self.assertFalse(calls.slot_is_full(self.org, ten))                             # cancelled calls don't count
        other_org = Organization.objects.create(name="Other", slug="other-slots")
        contact = Contact.objects.create(organization=other_org, phone="+16505559999")
        stranger = User.objects.create_user(email="s@other.test", password="pw-123456789", organization=other_org)
        calls.book_call(contact=contact, user=stranger, start=ten)
        self.assertFalse(calls.slot_is_full(self.org, ten))                             # another workspace's call

    def test_a_yes_to_a_time_that_filled_up_meanwhile_gets_the_next_one(self):
        conv = self.conv("CA")
        nine = self.tomorrow_at(9)
        Message.objects.create(organization=self.org, conversation=conv, direction=Message.Direction.OUTBOUND,
                               sender_type=Message.SenderType.AI, status=Message.Status.DELIVERED,
                               body="Would 9 AM tomorrow work?")
        self.say(conv, "tomorrow", "Great!", read={"date": nine.date().isoformat(), "their_words": "tomorrow"})
        suggested = datetime.fromisoformat(conv.ai_replies.order_by("-created_at").first().parsed_output["call_proposal"])
        calls.book_call(contact=self.other.contact, user=self.agent, start=suggested, conversation=self.other)
        reply = self.say(conv, "yes", "Great!")
        self.assertFalse(CalendarEvent.objects.filter(conversation=conv).exists())
        self.assertIn("fully booked", reply)
        self.assertIn(calls.proposal_text(suggested + timedelta(minutes=15), "America/Los_Angeles").split(" (")[0],
                      reply)

    def test_asap_skips_a_full_slot(self):
        now = timezone.now()
        first = calls.next_call_slot(now, "America/Los_Angeles", self.org)
        calls.book_call(contact=self.other.contact, user=self.agent, start=first, conversation=self.other)
        calls.book_call(contact=self.other.contact, user=self.agent, start=first + timedelta(minutes=15),
                        conversation=self.other)
        self.assertEqual(calls.next_call_slot(now, "America/Los_Angeles", self.org), first + timedelta(minutes=30))


class ClosedDayConditionsTests(TeamCallTestCase):
    def test_next_open_slot_jumps_over_a_closed_day(self):
        tomorrow = self.tomorrow_at(10).date()
        for d in (tomorrow, tomorrow + timedelta(days=1)):
            ClosedDay.objects.create(organization=self.org, date=d, name="Retreat")
        org = Organization.objects.get(pk=self.org.pk)
        evening = datetime.combine(tomorrow - timedelta(days=1), time_cls(22, 0), tzinfo=LA)   # after 9 PM
        slot = calls.next_call_slot(evening, "America/Los_Angeles", org)
        self.assertEqual(slot.astimezone(LA).date(), tomorrow + timedelta(days=2))

    def test_a_new_york_lead_on_the_teams_holiday(self):
        self.org.closed_holidays = ["christmas"]
        self.org.save()
        christmas_ny = datetime(2027, 12, 24, 13, 0, tzinfo=NY)          # observed Fri Dec 24, 2027
        self.assertFalse(calls.can_call_at(christmas_ny, "America/New_York", self.org))
        self.assertEqual(calls.closed_reason_at(christmas_ny, self.org), "Christmas Day")

    def test_owner_can_close_an_extra_holiday_in_settings_and_gemma_avoids_it(self):
        self.org.closed_holidays = ["columbus"]
        self.org.save()
        self.assertFalse(calls.can_call_at(datetime(2027, 10, 11, 10, 0, tzinfo=LA), "America/Los_Angeles", self.org))
        self.assertTrue(calls.can_call_at(datetime(2027, 11, 25, 10, 0, tzinfo=LA), "America/Los_Angeles", self.org))

    def test_a_long_lived_org_object_sees_a_newly_closed_day_within_seconds(self):
        from unittest.mock import patch
        day = date(2027, 3, 3)
        with patch("time.monotonic", return_value=1000.0):
            self.assertFalse(holidays.is_closed(self.org, day))               # cached now
        ClosedDay.objects.create(organization=self.org, date=day, name="Office party")
        with patch("time.monotonic", return_value=1000.0 + holidays.CACHE_SECONDS + 1):
            self.assertTrue(holidays.is_closed(self.org, day))

    def test_the_handoff_note_names_the_day_actually_booked(self):
        tomorrow = self.tomorrow_at(10)
        ClosedDay.objects.create(organization=self.org, date=tomorrow.date(), name="Thanksgiving")
        conv = self.conv("CA")
        self.say(conv, "call me tomorrow at 10am", "Sure!", needs_human=False,
                 read={"date": tomorrow.date().isoformat(), "time": "10:00", "their_words": "tomorrow at 10am"})
        self.say(conv, "yes", "Great!", reason="Wants a call - best time: tomorrow at 10am")
        friday = tomorrow + timedelta(days=1)
        note = Message.objects.filter(conversation=conv, body__startswith="Conversation handed off").last().body
        self.assertIn(f"{friday:%a, %b} {friday.day}", note)
        self.assertNotIn("tomorrow at 10am", note)

    def test_bad_closed_day_requests(self):
        self.client.force_login(self.owner)
        for data, message in (({"date": "nope", "action": "close"}, "Pick a valid date."),
                              ({"date": "2020-01-01", "action": "close"}, "That date has already passed."),
                              ({"date": "2027-03-03", "action": "explode"}, "Unknown action.")):
            self.assertEqual(self.client.post("/calendar/closed-days/", data).json()["message"], message)

    def test_reopened_holiday_shows_as_open_on_the_calendar(self):
        self.org.closed_holidays = ["thanksgiving"]
        self.org.save()
        ClosedDay.objects.create(organization=self.org, date=date(2027, 11, 25), closed=False, name="Thanksgiving")
        self.client.force_login(self.agent)
        feed = self.client.get("/calendar/events/", {"start": "2027-11-20T00:00:00", "end": "2027-11-30T00:00:00"}).json()
        [day] = [e for e in feed["events"] if e.get("display") == "background"]
        self.assertEqual((day["title"], day["allDay"], day["start"]), ("Open · Thanksgiving", True, "2027-11-25"))
        org = Organization.objects.get(pk=self.org.pk)
        self.assertTrue(calls.can_call_at(datetime(2027, 11, 25, 10, 0, tzinfo=LA), "America/Los_Angeles", org))

    def test_the_settings_page_lists_next_dates(self):
        rows = {r["key"]: r for r in holidays.upcoming_holidays(self.org, date(2026, 11, 25))}
        self.assertEqual(rows["thanksgiving"]["next"], date(2026, 11, 26))
        self.assertEqual(rows["new_year"]["next"], date(2027, 1, 1))
        self.assertEqual(rows["labor"]["next"], date(2027, 9, 6))                       # this year's has passed


@override_settings(MESSAGING_PROVIDER="fake", AI_CLIENT="fake")
class FourAgentsClaimAtOnceTests(TransactionTestCase):
    """Real database connections, 4 threads pressing Claim at the same instant: exactly one gets the call."""

    def test_only_one_agent_gets_it(self):
        org = Organization.objects.create(name="Race", slug="race", call_pool_enabled=True, closed_holidays=[])
        agents = [User.objects.create_user(email=f"a{i}@race.test", password="pw-123456789", organization=org,
                                           role=User.Role.AGENT, first_name=f"A{i}") for i in range(4)]
        phone = PhoneNumber.objects.create(organization=org, e164="+16505550100", is_active=True)
        contact = Contact.objects.create(organization=org, phone="+16505551111", sms_status=Contact.SMSStatus.OPTED_IN)
        conv = Conversation.objects.create(organization=org, contact=contact, phone_number=phone)
        event, _ = calls.book_call(contact=contact, user=None, start=timezone.now() + timedelta(days=1),
                                   conversation=conv)
        gate = Barrier(4)

        def press(agent):
            try:
                gate.wait()
                return calls.claim_call(CalendarEvent.objects.get(pk=event.pk), agent)
            finally:
                connection.close()

        from unittest.mock import patch
        with patch("messaging.ws_broadcast.broadcast_user_event"), patch("messaging.ws_broadcast.broadcast_chat_event"):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(press, agents))
        self.assertEqual(sorted(results), [False, False, False, True])
        event.refresh_from_db()
        conv.refresh_from_db()
        winner = agents[results.index(True)]
        self.assertEqual((event.user, conv.assigned_to), (winner, winner))
