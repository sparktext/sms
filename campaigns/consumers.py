import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer

logger = logging.getLogger(__name__)


class CampaignConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for live broadcast progress tracking at `/campaigns/<campaign_id>/`.
    Room group: `campaign_<campaign_id>`.
    Streams live progress bar movement, sent counts, carrier delivery receipts,
    and recipient status updates directly to connected browser clients with zero polling.
    """

    async def connect(self):
        self.user = self.scope.get("user")
        if self.user and not self.user.is_authenticated:
            await self.close()
            return

        self.campaign_id = self.scope["url_route"]["kwargs"]["campaign_id"]
        self.room_group_name = f"campaign_{self.campaign_id}"

        # Join the campaign broadcast room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )

        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name,
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            if data.get("type") == "ping":
                await self.send(text_data=json.dumps({"type": "pong"}))
        except Exception:
            pass

    async def campaign_progress(self, event):
        """
        Triggered when backend worker or delivery receipt updates campaign progress:
        channel_layer.group_send(room, {"type": "campaign_progress", "payload": ...})
        """
        await self.send(text_data=json.dumps(event["payload"]))

