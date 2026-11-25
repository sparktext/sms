import json
from datetime import UTC, datetime

from django.test import TestCase

from core.models import Organization, User, UserProfile
from core.timezones import user_timezone
from scheduling.models import CalendarEvent

UTC = UTC


class CalendarTestBase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Cal Co", slug="cal-co", timezone="America/Los_Angeles")
        self.owner = User.objects.create_user(
            email="owner@cal.test", password="pw-123456789", organization=self.org,
            role=User.Role.OWNER, first_name="Olivia",
        )
        self.admin = User.objects.create_user(
            email="admin@cal.test", password="pw-123456789", organization=self.org,
            role=User.Role.ADMIN, first_name="Adam",
        )
        self.agent = User.objects.create_user(
            email="agent@cal.test", password="pw-123456789", organization=self.org,
            role=User.Role.AGENT, first_name="Aaron",
        )
        self.other_org = Organization.objects.create(name="Other", slug="other-co", timezone="America/New_York")
        self.outsider = User.objects.create_user(
            email="outsider@other.test", password="pw-123456789", organization=self.other_org, role=User.Role.OWNER,
        )

    def post_json(self, url, data=None):
        return self.client.post(url, data=json.dumps(data or {}), content_type="application/json")

    def make_event(self, user, start, end, **extra):
        return CalendarEvent.objects.create(
            organization=user.organization, user=user, title=extra.pop("title", "Call"),
            start_at=start, end_at=end, **extra,
        )

    def feed(self, member=None, start="2026-12-01T00:00:00", end="2026-12-08T00:00:00"):
        params = {"start": start, "end": end}
        if member:
            params["member"] = member
        res = self.client.get("/calendar/events/", params)
        self.assertEqual(res.status_code, 200)
        return res.json()["events"]

    def set_personal_tz(self, user, tz):
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.timezone = tz
        profile.save()


class CalendarPageTests(CalendarTestBase):
    def test_every_role_has_a_calendar(self):
        for user in (self.owner, self.admin, self.agent):
            self.client.force_login(user)
            res = self.client.get("/calendar/")
            self.assertEqual(res.status_code, 200, user.role)
            self.assertContains(res, 'id="calendar"')
            self.assertContains(res, "Pacific Time")

    def test_only_owners_and_admins_can_switch_calendars(self):
        self.client.force_login(self.agent)
        self.assertNotContains(self.client.get("/calendar/"), 'id="cal-member"')
        self.client.force_login(self.admin)
        res = self.client.get("/calendar/")
        self.assertContains(res, 'id="cal-member"')
        self.assertContains(res, "Aaron")

    def test_sidebar_links_to_calendar(self):
        self.client.force_login(self.agent)
        self.assertContains(self.client.get("/dashboard/"), 'href="/calendar/"')


class CalendarTimezoneTests(CalendarTestBase):
    def test_new_event_uses_the_workspace_time_zone(self):
        self.client.force_login(self.agent)
        res = self.post_json("/calendar/events/create/", {"title": "Call Maria", "kind": "call",
                                                          "start": "2026-12-01T10:00", "end": "2026-12-01T10:30"})
        self.assertEqual(res.status_code, 200, res.content)
        event = CalendarEvent.objects.get(title="Call Maria")
        self.assertEqual(event.start_at, datetime(2026, 12, 1, 18, 0, tzinfo=UTC))   # 10:00 PST = 18:00 UTC
        self.assertEqual(event.user, self.agent)
        self.assertEqual(res.json()["event"]["start"], "2026-12-01T10:00:00-08:00")

    def test_daylight_saving_is_respected(self):
        self.client.force_login(self.agent)
        self.post_json("/calendar/events/create/", {"title": "Summer call", "start": "2026-07-01T10:00",
                                                    "end": "2026-07-01T10:30"})
        event = CalendarEvent.objects.get(title="Summer call")
        self.assertEqual(event.start_at, datetime(2026, 7, 1, 17, 0, tzinfo=UTC))    # 10:00 PDT = 17:00 UTC

    def test_personal_time_zone_overrides_the_workspace(self):
        self.client.force_login(self.agent)
        res = self.client.post("/calendar/timezone/", {"timezone": "Asia/Kolkata"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(user_timezone(User.objects.get(pk=self.agent.pk)), "Asia/Kolkata")
        self.post_json("/calendar/events/create/", {"title": "IST call", "start": "2026-12-01T10:00",
                                                    "end": "2026-12-01T10:30"})
        self.assertEqual(CalendarEvent.objects.get(title="IST call").start_at,
                         datetime(2026, 12, 1, 4, 30, tzinfo=UTC))                   # 10:00 IST = 04:30 UTC

        # Back to the workspace default
        self.client.post("/calendar/timezone/", {"timezone": ""})
        self.assertEqual(user_timezone(User.objects.get(pk=self.agent.pk)), "America/Los_Angeles")

    def test_unknown_time_zone_is_rejected(self):
        self.client.force_login(self.agent)
        res = self.client.post("/calendar/timezone/", {"timezone": "Mars/Olympus"})
        self.assertEqual(res.status_code, 400)

    def test_everyone_sees_the_same_moment_in_their_own_zone(self):
        self.make_event(self.agent, datetime(2026, 12, 1, 18, 0, tzinfo=UTC), datetime(2026, 12, 1, 18, 30, tzinfo=UTC))
        self.set_personal_tz(self.agent, "Asia/Kolkata")

        self.client.force_login(self.owner)                     # workspace zone: Los Angeles
        [seen_by_owner] = self.feed(member=str(self.agent.public_id))
        self.assertEqual(seen_by_owner["start"], "2026-12-01T10:00:00-08:00")

        self.client.force_login(self.agent)                     # own zone: India
        [seen_by_agent] = self.feed(end="2026-12-09T00:00:00")
        self.assertEqual(seen_by_agent["start"], "2026-12-01T23:30:00+05:30")

    def test_workspace_time_zone_is_set_in_company_profile(self):
        self.client.force_login(self.owner)
        page = self.client.get("/settings/profile/")
        self.assertContains(page, 'name="timezone"')
        self.client.post("/settings/profile/", {"name": "Cal Co", "timezone": "America/New_York", "address_state": "CA"})
        self.org.refresh_from_db()
        self.assertEqual(self.org.timezone, "America/New_York")
        self.assertEqual(user_timezone(User.objects.get(pk=self.agent.pk)), "America/New_York")

        self.client.post("/settings/profile/", {"name": "Cal Co", "timezone": "Not/AZone", "address_state": "CA"})
        self.org.refresh_from_db()
        self.assertEqual(self.org.timezone, "America/New_York")


class CalendarPermissionTests(CalendarTestBase):
    def setUp(self):
        super().setUp()
        start, end = datetime(2026, 12, 2, 17, 0, tzinfo=UTC), datetime(2026, 12, 2, 17, 30, tzinfo=UTC)
        self.owner_event = self.make_event(self.owner, start, end, title="Owner call")
        self.agent_event = self.make_event(self.agent, start, end, title="Agent call")
        self.outsider_event = self.make_event(self.outsider, start, end, title="Outsider call")

    def titles(self, events):
        return sorted(e["title"] for e in events)

    def test_agent_only_sees_their_own_calendar(self):
        self.client.force_login(self.agent)
        self.assertEqual(self.titles(self.feed()), ["Agent call"])
        self.assertEqual(self.titles(self.feed(member=str(self.owner.public_id))), ["Agent call"])
        self.assertEqual(self.titles(self.feed(member="all")), ["Agent call"])

    def test_owner_and_admin_can_open_anyones_calendar_or_everyone(self):
        for manager in (self.owner, self.admin):
            self.client.force_login(manager)
            self.assertEqual(self.titles(self.feed(member=str(self.agent.public_id))), ["Agent call"])
            self.assertEqual(self.titles(self.feed(member="all")), ["Agent call", "Owner call"])

    def test_other_workspaces_are_never_visible(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.titles(self.feed(member=str(self.outsider.public_id))), ["Owner call"])
        self.assertEqual(self.post_json(f"/calendar/events/{self.outsider_event.id}/update/", {"title": "x"}).status_code, 404)
        self.assertEqual(self.post_json(f"/calendar/events/{self.outsider_event.id}/delete/").status_code, 404)

    def test_agent_cannot_change_someone_elses_event(self):
        self.client.force_login(self.agent)
        self.assertEqual(self.post_json(f"/calendar/events/{self.owner_event.id}/update/", {"title": "x"}).status_code, 404)
        self.assertEqual(self.post_json(f"/calendar/events/{self.owner_event.id}/delete/").status_code, 404)
        self.assertTrue(CalendarEvent.objects.filter(pk=self.owner_event.pk, title="Owner call").exists())

    def test_agent_cannot_put_events_on_someone_elses_calendar(self):
        self.client.force_login(self.agent)
        self.post_json("/calendar/events/create/", {"title": "Sneaky", "member": str(self.owner.public_id),
                                                    "start": "2026-12-03T10:00", "end": "2026-12-03T10:30"})
        self.assertEqual(CalendarEvent.objects.get(title="Sneaky").user, self.agent)

    def test_manager_can_schedule_for_a_team_member_and_reassign(self):
        self.client.force_login(self.admin)
        res = self.post_json("/calendar/events/create/", {"title": "For Aaron", "member": str(self.agent.public_id),
                                                          "start": "2026-12-03T10:00", "end": "2026-12-03T10:30"})
        self.assertEqual(res.status_code, 200)
        event = CalendarEvent.objects.get(title="For Aaron")
        self.assertEqual((event.user, event.created_by), (self.agent, self.admin))

        self.post_json(f"/calendar/events/{event.id}/update/", {"member": str(self.owner.public_id)})
        event.refresh_from_db()
        self.assertEqual(event.user, self.owner)

    def test_removed_members_drop_out_of_everyone_view(self):
        self.agent.is_active = False
        self.agent.save()
        self.client.force_login(self.owner)
        self.assertEqual(self.titles(self.feed(member="all")), ["Owner call"])


class CalendarEditTests(CalendarTestBase):
    def setUp(self):
        super().setUp()
        self.event = self.make_event(self.agent, datetime(2026, 12, 2, 17, 0, tzinfo=UTC),
                                     datetime(2026, 12, 2, 17, 30, tzinfo=UTC), title="Call Maria", notes="Wants auto")
        self.client.force_login(self.agent)

    def test_dragging_moves_the_event_and_keeps_everything_else(self):
        res = self.post_json(f"/calendar/events/{self.event.id}/update/",
                             {"start": "2026-12-03T09:00:00-08:00", "end": "2026-12-03T10:00:00-08:00"})
        self.assertEqual(res.status_code, 200, res.content)
        self.event.refresh_from_db()
        self.assertEqual(self.event.start_at, datetime(2026, 12, 3, 17, 0, tzinfo=UTC))
        self.assertEqual(self.event.end_at, datetime(2026, 12, 3, 18, 0, tzinfo=UTC))
        self.assertEqual((self.event.title, self.event.notes), ("Call Maria", "Wants auto"))

    def test_mark_completed_and_delete(self):
        self.post_json(f"/calendar/events/{self.event.id}/update/", {"status": "completed"})
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, CalendarEvent.Status.COMPLETED)
        self.assertEqual(self.post_json(f"/calendar/events/{self.event.id}/delete/").status_code, 200)
        self.assertFalse(CalendarEvent.objects.filter(pk=self.event.pk).exists())

    def test_bad_input_is_rejected_with_a_clear_message(self):
        cases = [
            ({"start": "2026-12-03T10:00", "end": "2026-12-03T09:00"}, "end time must be after"),
            ({"start": "2026-12-03T10:00", "end": "2026-12-20T10:00"}, "can't be longer"),
            ({"start": "nope", "end": "2026-12-03T10:00"}, "valid start"),
            ({"start": "2026-12-03T10:00", "end": "2026-12-03T11:00", "kind": "party"}, "Unknown event type"),
        ]
        for data, message in cases:
            res = self.post_json("/calendar/events/create/", {"title": "Bad", **data})
            self.assertEqual(res.status_code, 400, data)
            self.assertIn(message, res.json()["message"])
        self.assertFalse(CalendarEvent.objects.filter(title="Bad").exists())

    def test_blank_title_falls_back_to_the_type(self):
        self.post_json("/calendar/events/create/", {"title": "", "kind": "follow_up",
                                                    "start": "2026-12-03T10:00", "end": "2026-12-03T10:15"})
        self.assertTrue(CalendarEvent.objects.filter(title="Follow-up", kind="follow_up").exists())

    def test_feed_only_returns_events_in_the_window(self):
        self.make_event(self.agent, datetime(2027, 1, 5, 17, 0, tzinfo=UTC), datetime(2027, 1, 5, 18, 0, tzinfo=UTC),
                        title="Next month")
        self.assertEqual([e["title"] for e in self.feed()], ["Call Maria"])
