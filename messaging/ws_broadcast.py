import asyncio
import logging
from typing import Any, Dict
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def broadcast_chat_event(conversation_id: int, payload: Dict[str, Any]) -> None:
    """
    Broadcasts a real-time event to all WebSocket clients connected to `chat_<conversation_id>`.
    Works seamlessly from both synchronous code (Django views, workers) and async event loops.

    Example payloads:
    1. New message:
       {
           "event": "new_message",
           "message": {
               "id": 101,
               "body": "Hello!",
               "direction": "inbound",
               "sender_type": "customer",
               "created_at": "10:30 AM",
               "status": "received"
           }
       }

    2. Delivery status update:
       {
           "event": "status_update",
           "message_id": 101,
           "status": "delivered"
       }
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        message_dict = {
            "type": "chat_message",
            "payload": payload,
        }

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Running inside an active asyncio event loop
            asyncio.create_task(channel_layer.group_send(f"chat_{conversation_id}", message_dict))
        else:
            # Synchronous Django view or thread
            async_to_sync(channel_layer.group_send)(
                f"chat_{conversation_id}",
                message_dict,
            )
    except Exception as exc:
        logger.warning(
            "WebSocket broadcast error for conversation %s: %s",
            conversation_id,
            exc,
            exc_info=False,
        )


async def async_broadcast_chat_event(conversation_id: int, payload: Dict[str, Any]) -> None:
    """
    Direct coroutine broadcast for async views and consumers.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        await channel_layer.group_send(
            f"chat_{conversation_id}",
            {
                "type": "chat_message",
                "payload": payload,
            },
        )
    except Exception as exc:
        logger.warning(
            "Async WebSocket broadcast error for conversation %s: %s",
            conversation_id,
            exc,
            exc_info=False,
        )

