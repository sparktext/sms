from django.test import SimpleTestCase, override_settings
from django.contrib.auth.models import AnonymousUser
from channels.testing import WebsocketCommunicator
from messaging.consumers import ChatConsumer, InboxConsumer
from messaging.ws_broadcast import (
    async_broadcast_chat_event,
    async_broadcast_inbox_event,
    async_broadcast_user_event,
)
from campaigns.consumers import CampaignConsumer
from campaigns.ws_broadcast import async_broadcast_campaign_progress

TEST_CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}


class MockUser:
    is_authenticated = True
    id = 42
    first_name = "Alice"
    email = "agent@example.com"
    organization_id = 99


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class ChatConsumerSimpleTests(SimpleTestCase):
    async def test_authenticated_user_connects_and_receives_broadcast(self):
        conv_id = 123
        communicator = WebsocketCommunicator(
            ChatConsumer.as_asgi(),
            f"/ws/chat/{conv_id}/"
        )
        communicator.scope["url_route"] = {"kwargs": {"conversation_id": conv_id}}
        communicator.scope["user"] = MockUser()

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Test ping-pong heartbeat
        await communicator.send_json_to({"type": "ping"})
        response = await communicator.receive_json_from()
        self.assertEqual(response, {"type": "pong"})

        # Test broadcast event delivery via async_broadcast_chat_event
        await async_broadcast_chat_event(
            conv_id,
            {
                "event": "new_message",
                "message": {
                    "id": 999,
                    "body": "Live test message",
                    "direction": "inbound",
                    "sender_type": "customer",
                    "created_at": "12:00 PM",
                    "status": "received",
                },
            },
        )

        ws_msg = await communicator.receive_json_from()
        self.assertEqual(ws_msg.get("event"), "new_message")
        self.assertEqual(ws_msg.get("message", {}).get("body"), "Live test message")

        # Test ai_typing broadcast event
        await async_broadcast_chat_event(
            conv_id,
            {
                "event": "ai_typing",
                "conversation_id": conv_id,
                "is_typing": True,
                "model_name": "Gemma",
            },
        )
        ws_typing = await communicator.receive_json_from()
        self.assertEqual(ws_typing.get("event"), "ai_typing")
        self.assertTrue(ws_typing.get("is_typing"))
        self.assertEqual(ws_typing.get("model_name"), "Gemma")

        # Test ai_draft broadcast event
        await async_broadcast_chat_event(
            conv_id,
            {
                "event": "ai_draft",
                "conversation_id": conv_id,
                "draft": "Here is a suggested quote reply.",
            },
        )
        ws_draft = await communicator.receive_json_from()
        self.assertEqual(ws_draft.get("event"), "ai_draft")
        self.assertEqual(ws_draft.get("draft"), "Here is a suggested quote reply.")

        # Test handoff broadcast event
        await async_broadcast_chat_event(
            conv_id,
            {
                "event": "handoff",
                "conversation_id": conv_id,
                "status": "needs_human",
                "reason": "Customer inquiry requires human assistance.",
            },
        )
        ws_handoff = await communicator.receive_json_from()
        self.assertEqual(ws_handoff.get("event"), "handoff")
        self.assertEqual(ws_handoff.get("status"), "needs_human")
        self.assertEqual(ws_handoff.get("reason"), "Customer inquiry requires human assistance.")

        # Test agent collision typing signal from client
        await communicator.send_json_to({"type": "agent_typing", "is_typing": True})
        ws_agent_typing = await communicator.receive_json_from()
        self.assertEqual(ws_agent_typing.get("event"), "agent_typing")
        self.assertTrue(ws_agent_typing.get("is_typing"))
        self.assertEqual(ws_agent_typing.get("user_id"), MockUser.id)

        await communicator.disconnect()

    async def test_unauthenticated_user_is_disconnected(self):
        conv_id = 456
        communicator = WebsocketCommunicator(
            ChatConsumer.as_asgi(),
            f"/ws/chat/{conv_id}/"
        )
        communicator.scope["url_route"] = {"kwargs": {"conversation_id": conv_id}}
        communicator.scope["user"] = AnonymousUser()

        connected, _ = await communicator.connect()
        self.assertFalse(connected)

    async def test_inbox_consumer_receives_update(self):
        org_id = 99
        communicator = WebsocketCommunicator(
            InboxConsumer.as_asgi(),
            f"/ws/inbox/{org_id}/"
        )
        communicator.scope["url_route"] = {"kwargs": {"org_id": org_id}}
        communicator.scope["user"] = MockUser()

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        await async_broadcast_inbox_event(
            org_id,
            {
                "event": "inbox_update",
                "conversation": {
                    "id": 101,
                    "last_body": "Hello from customer",
                    "direction": "inbound",
                    "last_message_at_display": "just now",
                    "unread_count": 2,
                },
                "nav_unread_count": 5,
                "toast": {
                    "title": "New message from Jane Doe",
                    "body": "Hello from customer",
                    "conversation_id": 101,
                    "type": "inbound_message",
                },
            },
        )

        ws_msg = await communicator.receive_json_from()
        self.assertEqual(ws_msg.get("event"), "inbox_update")
        self.assertEqual(ws_msg.get("conversation", {}).get("last_body"), "Hello from customer")
        self.assertEqual(ws_msg.get("nav_unread_count"), 5)
        self.assertEqual(ws_msg.get("toast", {}).get("title"), "New message from Jane Doe")

        # Test direct personal user broadcast for lead assignment
        user_id = MockUser.id
        await async_broadcast_user_event(
            user_id,
            {
                "event": "inbox_update",
                "conversation": {
                    "id": 202,
                    "last_body": "Lead automatically assigned to Alice",
                    "direction": "outbound",
                    "last_message_at_display": "just now",
                    "assigned_to_id": user_id,
                },
                "toast": {
                    "type": "lead_assigned",
                    "title": "🎯 New Qualified Lead Assigned",
                    "body": "Sam Taylor (+16505550009)",
                    "conversation_id": 202,
                    "contact_name": "Sam Taylor",
                    "contact_phone": "+16505550009",
                },
            },
        )
        ws_user_msg = await communicator.receive_json_from()
        self.assertEqual(ws_user_msg.get("event"), "inbox_update")
        self.assertEqual(ws_user_msg.get("toast", {}).get("type"), "lead_assigned")
        self.assertEqual(ws_user_msg.get("toast", {}).get("title"), "🎯 New Qualified Lead Assigned")
        self.assertEqual(ws_user_msg.get("toast", {}).get("body"), "Sam Taylor (+16505550009)")
        self.assertEqual(ws_user_msg.get("toast", {}).get("conversation_id"), 202)

        await communicator.disconnect()

    async def test_campaign_consumer_receives_live_progress_broadcast(self):
        campaign_id = 55
        communicator = WebsocketCommunicator(
            CampaignConsumer.as_asgi(),
            f"/ws/campaign/{campaign_id}/",
        )
        communicator.scope["url_route"] = {"kwargs": {"campaign_id": campaign_id}}
        communicator.scope["user"] = MockUser()

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Test ping-pong heartbeat
        await communicator.send_json_to({"type": "ping"})
        response = await communicator.receive_json_from()
        self.assertEqual(response, {"type": "pong"})

        # Broadcast live progress event
        await async_broadcast_campaign_progress(
            campaign_id,
            {
                "event": "campaign_progress",
                "campaign_id": campaign_id,
                "status": "sending",
                "status_display": "Sending...",
                "progress_percent": 45,
                "delivery_rate": 88,
                "total_recipients": 500,
                "sent_count": 225,
                "delivered_count": 198,
                "failed_count": 5,
                "skipped_count": 20,
                "is_completed": False,
                "recipients": [
                    {
                        "id": 101,
                        "status": "sent",
                        "status_display": "Sent",
                    }
                ],
            },
        )

        ws_msg = await communicator.receive_json_from()
        self.assertEqual(ws_msg.get("event"), "campaign_progress")
        self.assertEqual(ws_msg.get("progress_percent"), 45)
        self.assertEqual(ws_msg.get("sent_count"), 225)
        self.assertEqual(ws_msg.get("delivery_rate"), 88)
        self.assertEqual(len(ws_msg.get("recipients", [])), 1)

        await communicator.disconnect()

    async def test_campaign_consumer_rejects_unauthenticated(self):
        campaign_id = 77
        communicator = WebsocketCommunicator(
            CampaignConsumer.as_asgi(),
            f"/ws/campaign/{campaign_id}/",
        )
        communicator.scope["url_route"] = {"kwargs": {"campaign_id": campaign_id}}
        communicator.scope["user"] = AnonymousUser()

        connected, _ = await communicator.connect()
        self.assertFalse(connected)

