from django.test import SimpleTestCase
from django.contrib.auth.models import AnonymousUser
from channels.testing import WebsocketCommunicator
from messaging.consumers import ChatConsumer
from messaging.ws_broadcast import async_broadcast_chat_event


class MockUser:
    is_authenticated = True
    id = 42
    email = "agent@example.com"


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
