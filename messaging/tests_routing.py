from django.test import TestCase, Client
from django.utils import timezone
from core.models import Organization, User
from contacts.models import Contact
from messaging.models import Conversation, Message, PhoneNumber
from messaging.routing import assign_qualified_lead


class LeadRoutingTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Apex Insurance",
            slug="apex-ins",
            lead_routing_strategy=Organization.LeadRoutingStrategy.WORKLOAD_BALANCED,
            max_leads_per_agent=3,
            route_only_available=True,
        )
        self.owner = User.objects.create_user(
            email="owner@apex.com",
            password="Password123!",
            first_name="Alice",
            last_name="Owner",
            role=User.Role.OWNER,
            organization=self.org,
            is_available_for_leads=True,
        )
        self.agent_bob = User.objects.create_user(
            email="bob@apex.com",
            password="Password123!",
            first_name="Bob",
            last_name="Agent",
            role=User.Role.AGENT,
            organization=self.org,
            is_available_for_leads=True,
        )
        self.agent_carol = User.objects.create_user(
            email="carol@apex.com",
            password="Password123!",
            first_name="Carol",
            last_name="Agent",
            role=User.Role.AGENT,
            organization=self.org,
            is_available_for_leads=True,
        )

        self.phone = PhoneNumber.objects.create(
            organization=self.org,
            e164="+16505550100",
            is_active=True,
            is_default=True,
        )

        self.contact = Contact.objects.create(
            organization=self.org,
            phone="+16505559999",
            first_name="Dan",
            last_name="Customer",
        )

        self.conv = Conversation.objects.create(
            organization=self.org,
            contact=self.contact,
            phone_number=self.phone,
            status=Conversation.Status.NEEDS_HUMAN,
        )

        self.client = Client()

    def test_workload_balanced_assigns_to_least_busy_agent(self):
        # Give Bob 1 open conversation
        c_bob = Contact.objects.create(organization=self.org, phone="+16505550001")
        Conversation.objects.create(
            organization=self.org,
            contact=c_bob,
            phone_number=self.phone,
            status=Conversation.Status.OPEN,
            assigned_to=self.agent_bob,
        )

        # Give Alice (Owner) 2 open conversations
        c_alice1 = Contact.objects.create(organization=self.org, phone="+16505550002")
        Conversation.objects.create(
            organization=self.org,
            contact=c_alice1,
            phone_number=self.phone,
            status=Conversation.Status.OPEN,
            assigned_to=self.owner,
        )

        # Carol has 0 conversations. Lead should go to Carol!
        assigned = assign_qualified_lead(self.conv)
        self.assertEqual(assigned, self.agent_carol)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.assigned_to, self.agent_carol)

        # Verify system audit message was created
        msg = self.conv.messages.filter(sender_type=Message.SenderType.SYSTEM).last()
        self.assertIsNotNone(msg)
        self.assertIn("Carol", msg.body)
        self.assertIn("Workload-Balanced", msg.body)

    def test_concurrency_cap_bypasses_capped_agent(self):
        self.org.max_leads_per_agent = 1
        self.org.save()

        # Bob and Carol both have 1 open conversation (at cap!)
        c1 = Contact.objects.create(organization=self.org, phone="+16505550010")
        Conversation.objects.create(
            organization=self.org,
            contact=c1,
            phone_number=self.phone,
            status=Conversation.Status.OPEN,
            assigned_to=self.agent_bob,
        )
        c2 = Contact.objects.create(organization=self.org, phone="+16505550011")
        Conversation.objects.create(
            organization=self.org,
            contact=c2,
            phone_number=self.phone,
            status=Conversation.Status.OPEN,
            assigned_to=self.agent_carol,
        )

        # Alice has 0 conversations. Lead must bypass Bob & Carol and go to Alice!
        assigned = assign_qualified_lead(self.conv)
        self.assertEqual(assigned, self.owner)

    def test_all_agents_at_cap_leaves_lead_unassigned(self):
        self.org.max_leads_per_agent = 1
        self.org.save()

        # All 3 users have 1 open conversation
        for u, phone_suffix in [(self.owner, "1"), (self.agent_bob, "2"), (self.agent_carol, "3")]:
            c = Contact.objects.create(organization=self.org, phone=f"+1650555009{phone_suffix}")
            Conversation.objects.create(
                organization=self.org,
                contact=c,
                phone_number=self.phone,
                status=Conversation.Status.OPEN,
                assigned_to=u,
            )

        assigned = assign_qualified_lead(self.conv)
        self.assertIsNone(assigned)
        self.conv.refresh_from_db()
        self.assertIsNone(self.conv.assigned_to)

        # Audit message informs team they are at max cap
        msg = self.conv.messages.filter(sender_type=Message.SenderType.SYSTEM).last()
        self.assertIsNotNone(msg)
        self.assertIn("maximum capacity", msg.body)

    def test_bypasses_away_agents(self):
        # Mark Carol and Bob as Away
        self.agent_carol.is_available_for_leads = False
        self.agent_carol.save()
        self.agent_bob.is_available_for_leads = False
        self.agent_bob.save()

        # Even if legacy DB flag route_only_available was False, routing strictly enforces availability
        self.org.route_only_available = False
        self.org.save()

        # Only Alice is Available
        assigned = assign_qualified_lead(self.conv)
        self.assertEqual(assigned, self.owner)

    def test_round_robin_rotation(self):
        self.org.lead_routing_strategy = Organization.LeadRoutingStrategy.ROUND_ROBIN
        self.org.save()

        conv1 = self.conv
        assigned1 = assign_qualified_lead(conv1)
        self.assertIsNotNone(assigned1)

        c2 = Contact.objects.create(organization=self.org, phone="+16505550020")
        conv2 = Conversation.objects.create(organization=self.org, contact=c2, phone_number=self.phone)
        assigned2 = assign_qualified_lead(conv2)
        self.assertIsNotNone(assigned2)
        self.assertNotEqual(assigned1, assigned2)

        c3 = Contact.objects.create(organization=self.org, phone="+16505550021")
        conv3 = Conversation.objects.create(organization=self.org, contact=c3, phone_number=self.phone)
        assigned3 = assign_qualified_lead(conv3)
        self.assertIsNotNone(assigned3)
        self.assertNotEqual(assigned2, assigned3)

        # 4th lead cycles back to first assigned
        c4 = Contact.objects.create(organization=self.org, phone="+16505550022")
        conv4 = Conversation.objects.create(organization=self.org, contact=c4, phone_number=self.phone)
        assigned4 = assign_qualified_lead(conv4)
        self.assertEqual(assigned1, assigned4)

    def test_disabled_strategy_leaves_unassigned(self):
        self.org.lead_routing_strategy = Organization.LeadRoutingStrategy.DISABLED
        self.org.save()

        assigned = assign_qualified_lead(self.conv)
        self.assertIsNone(assigned)
        self.conv.refresh_from_db()
        self.assertIsNone(self.conv.assigned_to)

    def test_lead_routing_settings_view(self):
        self.client.force_login(self.owner)
        resp = self.client.get("/settings/routing/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Lead Distribution Strategy")
        self.assertContains(resp, "Workload-Balanced")
        self.assertContains(resp, "Round-Robin")
        self.assertNotContains(resp, 'name="route_only_available"')

        # Update settings to Round-Robin and cap of 8 (without checkbox parameter)
        post_resp = self.client.post("/settings/routing/", {
            "lead_routing_strategy": "round_robin",
            "max_leads_per_agent": "8",
        })
        self.assertEqual(post_resp.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.lead_routing_strategy, "round_robin")
        self.assertEqual(self.org.max_leads_per_agent, 8)
        self.assertTrue(self.org.route_only_available)

    def test_toggle_availability_view(self):
        self.client.force_login(self.agent_bob)
        self.assertTrue(self.agent_bob.is_available_for_leads)

        # Agent toggles own availability
        resp = self.client.post("/settings/availability/toggle/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["is_available"])
        self.agent_bob.refresh_from_db()
        self.assertFalse(self.agent_bob.is_available_for_leads)

        # Toggle back
        resp2 = self.client.post("/settings/availability/toggle/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp2.status_code, 200)
        self.agent_bob.refresh_from_db()
        self.assertTrue(self.agent_bob.is_available_for_leads)
