import asyncio
import logging
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def broadcast_campaign_progress(
    campaign,
    recipient_updates: list[dict[str, Any]] | None = None,
) -> None:
    """
    Broadcasts real-time campaign progress events to all WebSocket clients watching `campaign_<campaign_id>`.
    Updates progress bar width, delivery rates, metrics cards, and individual recipient statuses live.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        payload = {
            "event": "campaign_progress",
            "campaign_id": campaign.id,
            "name": campaign.name,
            "status": campaign.status,
            "status_display": campaign.get_status_display(),
            "progress_percent": campaign.progress_percent,
            "delivery_rate": campaign.delivery_rate,
            "total_recipients": campaign.total_recipients,
            "sent_count": campaign.sent_count,
            "delivered_count": campaign.delivered_count,
            "failed_count": campaign.failed_count,
            "skipped_count": campaign.skipped_count,
            "is_completed": campaign.status == "completed",
            "is_cancelled": campaign.status == "cancelled",
            "is_sending": campaign.status == "sending",
            "recipients": recipient_updates or [],
        }

        message_dict = {
            "type": "campaign_progress",
            "payload": payload,
        }

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        group_name = f"campaign_{campaign.id}"

        if loop and loop.is_running():
            asyncio.create_task(channel_layer.group_send(group_name, message_dict))
        else:
            async_to_sync(channel_layer.group_send)(group_name, message_dict)

    except Exception as exc:
        logger.warning(
            "WebSocket broadcast error for campaign %s: %s",
            getattr(campaign, "id", None),
            exc,
            exc_info=False,
        )


def broadcast_campaign_list_update(
    campaign,
    action: str = "created",
) -> None:
    """
    Broadcasts campaign creation, deletion, or major status change to the organization
    channel (`inbox_<org_id>`), allowing active users on /campaigns/ to see the new campaign
    row and updated metrics immediately without a manual browser refresh.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer or not getattr(campaign, "organization_id", None):
            return

        lists_summary = [lst.name for lst in campaign.lists.all()] if hasattr(campaign, "lists") else []
        phone_e164 = campaign.phone_number.e164 if campaign.phone_number else ""
        phone_label = campaign.phone_number.label if campaign.phone_number and campaign.phone_number.label else ""

        payload = {
            "event": "campaign_list_update",
            "action": action,
            "campaign": {
                "id": campaign.id,
                "name": campaign.name,
                "status": campaign.status,
                "status_display": campaign.get_status_display(),
                "phone_e164": phone_e164,
                "phone_label": phone_label,
                "lists": lists_summary,
                "total_recipients": campaign.total_recipients,
                "sent_count": campaign.sent_count,
                "skipped_count": campaign.skipped_count,
                "progress_percent": campaign.progress_percent,
                "created_at_date": _local(campaign, campaign.created_at, "%b %d, %Y"),
                "scheduled_at_date": _local(campaign, campaign.scheduled_at, "%b %d, %Y"),
                "scheduled_at_time": _local(campaign, campaign.scheduled_at, "%I:%M %p"),
                "detail_url": f"/campaigns/{campaign.id}/",
                "edit_url": f"/campaigns/{campaign.id}/edit/",
                "clone_url": f"/campaigns/{campaign.id}/clone/",
            },
        }

        message_dict = {
            "type": "inbox_update",
            "payload": payload,
        }

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        group_name = f"inbox_{campaign.organization_id}"

        if loop and loop.is_running():
            asyncio.create_task(channel_layer.group_send(group_name, message_dict))
        else:
            async_to_sync(channel_layer.group_send)(group_name, message_dict)

    except Exception as exc:
        logger.warning("Error broadcasting campaign_list_update: %s", exc)


async def async_broadcast_campaign_progress(
    campaign_id: int,
    payload: dict[str, Any],
) -> None:
    """
    Direct async broadcast helper for coroutines, consumers, and tests.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        await channel_layer.group_send(
            f"campaign_{campaign_id}",
            {
                "type": "campaign_progress",
                "payload": payload,
            },
        )
    except Exception as exc:
        logger.warning(
            "Async WebSocket broadcast error for campaign %s: %s",
            campaign_id,
            exc,
            exc_info=False,
        )


def _local(campaign, moment, fmt: str) -> str:
    """A campaign time in the workspace time zone (not the server's UTC)."""
    if not moment:
        return ""
    from zoneinfo import ZoneInfo

    from django.utils import timezone as dj_tz

    from core.timezones import org_timezone
    return dj_tz.localtime(moment, ZoneInfo(org_timezone(campaign.organization))).strftime(fmt)
