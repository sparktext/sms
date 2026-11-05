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
        """
        try:
            data = json.loads(text_data)
            msg_type = data.get("type", "ping")
            if msg_type == "ping":
                await self.send(text_data=json.dumps({"type": "pong"}))
        except Exception:
            pass

    async def chat_message(self, event):
        """
        Triggered when backend code (e.g., Telnyx webhook or send_message service)
        calls channel_layer.group_send(room, {"type": "chat_message", "payload": ...}).
        Pushes the message event to the connected browser.
        """
        await self.send(text_data=json.dumps(event["payload"]))


