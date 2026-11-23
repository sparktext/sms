import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer

logger = logging.getLogger(__name__)


class ChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time two-way SMS messaging in a conversation.
    Each conversation has its own room group: `chat_<conversation_id>`.
    """

    async def connect(self):
        self.user = self.scope.get("user")
        # Ensure only authenticated users can connect to chat channels
        if self.user and not self.user.is_authenticated:
            await self.close()
            return

        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.room_group_name = f"chat_{self.conversation_id}"

        # Join the conversation's broadcast group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )

        # Accept the incoming WebSocket handshake
        await self.accept()

    async def disconnect(self, close_code):
        # Leave the conversation's broadcast group
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name,
            )

    async def receive(self, text_data):
        """
        Handles incoming messages sent from the browser over WebSocket.
        Supports heartbeat pings and agent collision typing signals.
        """
        try:
            data = json.loads(text_data)
            msg_type = data.get("type", "ping")
            if msg_type == "ping":
                await self.send(text_data=json.dumps({"type": "pong"}))
            elif msg_type == "agent_typing":
                is_typing = bool(data.get("is_typing", False))
                user_name = "An agent"
                user_id = None
                if self.user and self.user.is_authenticated:
                    user_id = getattr(self.user, "id", None)
                    fn = getattr(self.user, "first_name", "")
                    em = getattr(self.user, "email", "")
                    user_name = fn or (em.split("@")[0] if em else "Agent")

                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "chat_message",
                        "payload": {
                            "event": "agent_typing",
                            "user_id": user_id,
                            "user_name": user_name,
                            "is_typing": is_typing,
                        },
                    },
                )
        except Exception as exc:
            logger.debug("ChatConsumer receive error: %s", exc)

    async def chat_message(self, event):
        """
        Triggered when backend code (e.g., Telnyx webhook or send_message service)
        calls channel_layer.group_send(room, {"type": "chat_message", "payload": ...}).
        Pushes the message event to the connected browser.
        """
        await self.send(text_data=json.dumps(event["payload"]))


class InboxConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for organization-level real-time inbox updates.
    Broadcast group: `inbox_<org_id>`.
    Used by the sidebar in inbox.html to update conversation snippets,
    unread counts, and move cards to the top in real time with ZERO polling.
    """

    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            await self.close()
            return

        self.org_id = self.scope["url_route"]["kwargs"]["org_id"]
        user_org_id = getattr(self.user, "organization_id", None)
        if user_org_id is not None and str(user_org_id) != str(self.org_id):
            await self.close()
            return

        self.room_group_name = f"inbox_{self.org_id}"
        self.user_group_name = f"user_{self.user.id}"

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )
        await self.channel_layer.group_add(
            self.user_group_name,
            self.channel_name,
        )
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name,
            )
        if hasattr(self, "user_group_name"):
            await self.channel_layer.group_discard(
                self.user_group_name,
                self.channel_name,
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            if data.get("type") == "ping":
                await self.send(text_data=json.dumps({"type": "pong"}))
        except Exception:
            pass

    async def inbox_update(self, event):
        """
        Triggered when backend code calls:
        channel_layer.group_send("inbox_<org_id>", {"type": "inbox_update", "payload": ...}).
        Pushes the conversation update event to the connected browser sidebar.
        """
        await self.send(text_data=json.dumps(event["payload"]))



