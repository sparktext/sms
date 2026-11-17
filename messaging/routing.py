import logging
from typing import Optional
from django.utils import timezone
from django.db.models import Count, Q

from core.models import Organization, User
from messaging.models import Conversation, Message

logger = logging.getLogger(__name__)


def assign_qualified_lead(
    conversation: Conversation,
    trigger_reason: str = "Lead qualified",
) -> Optional[User]:
    """
    Automated Lead Distribution Engine.
    Routes a qualified conversation to the appropriate sales team member based
    on the organization's configured strategy:
    - WORKLOAD_BALANCED (Least busy rep with fair tiebreaker & capacity cap)
    - ROUND_ROBIN (Sequential rotation among available reps)
    - DISABLED (Manual team claiming)

    Returns the assigned User, or None if unassigned or disabled.
    """
    if conversation.assigned_to:
        logger.info(
            "Conversation #%d is already assigned to %s. Skipping auto-routing.",
            conversation.id,
            conversation.assigned_to.email,
        )
        return conversation.assigned_to

    org = conversation.organization
    if not org:
        return None

    strategy = org.lead_routing_strategy
    if strategy == Organization.LeadRoutingStrategy.DISABLED:
        logger.info("Organization '%s' has lead routing disabled (manual queue).", org.name)
        return None

    # Base eligible users: active users with agent/admin/owner roles
    eligible_users = list(
        User.objects.filter(
            organization=org,
            is_active=True,
            role__in=[User.Role.AGENT, User.Role.ADMIN, User.Role.OWNER],
        ).order_by("id")
    )

    if not eligible_users:
        logger.warning("No active team members found in organization '%s' for lead routing.", org.name)
        return None

    # Strictly route only to team members currently toggled Available for leads
    eligible_users = [u for u in eligible_users if u.is_available_for_leads]
    if not eligible_users:
        logger.info("All team members in '%s' are currently marked Away. Lead remains unassigned.", org.name)
        Message.objects.create(
            conversation=conversation,
            organization=org,
            sender_type=Message.SenderType.SYSTEM,
            direction=Message.Direction.OUTBOUND,
            status=Message.Status.DELIVERED,
            body="All sales team members are currently marked Away. Lead placed in shared unassigned queue.",
        )
        return None

    # Compute current active workloads (open or needs_human conversations)
    active_statuses = [Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN]
    user_ids = [u.id for u in eligible_users]
    workload_counts = dict(
        Conversation.objects.filter(
            organization=org,
            assigned_to_id__in=user_ids,
            status__in=active_statuses,
        )
        .values("assigned_to_id")
        .annotate(total=Count("id"))
        .values_list("assigned_to_id", "total")
    )

    candidates = []
    max_cap = org.max_leads_per_agent or 0

    for u in eligible_users:
        load = workload_counts.get(u.id, 0)
        # Check concurrency cap
        if max_cap > 0 and load >= max_cap:
            continue
        candidates.append((u, load))

    if not candidates:
        logger.info(
            "All available team members in '%s' have reached maximum capacity (%d leads).",
            org.name,
            max_cap,
        )
        Message.objects.create(
            conversation=conversation,
            organization=org,
            sender_type=Message.SenderType.SYSTEM,
            direction=Message.Direction.OUTBOUND,
            status=Message.Status.DELIVERED,
            body=f"All sales team members are at maximum capacity ({max_cap} active leads). Lead placed in shared unassigned queue.",
        )
        return None

    chosen_user = None
    chosen_load = 0
    strat_label = ""

    if strategy == Organization.LeadRoutingStrategy.WORKLOAD_BALANCED:
        strat_label = "Workload-Balanced"
        # Sort key:
        # 1. lowest active load
        # 2. oldest last_lead_assigned_at (None comes first so new/idle reps get leads first)
        # 3. lowest user ID
        def _sort_key(item):
            user, load = item
            assigned_at = user.last_lead_assigned_at.timestamp() if user.last_lead_assigned_at else 0
            return (load, assigned_at, user.id)

        candidates.sort(key=_sort_key)
        chosen_user, chosen_load = candidates[0]

    elif strategy == Organization.LeadRoutingStrategy.ROUND_ROBIN:
        strat_label = "Round-Robin"
        # Determine next user in circular sequence
        candidate_users = [c[0] for c in candidates]
        last_id = org.last_assigned_user_id

        next_idx = 0
        if last_id is not None:
            # Find index of last assigned user if in candidates, or position relative to it
            matched_idx = -1
            for idx, u in enumerate(candidate_users):
                if u.id == last_id:
                    matched_idx = idx
                    break

            if matched_idx != -1:
                next_idx = (matched_idx + 1) % len(candidate_users)
            else:
                # Last assigned user is not currently a candidate (e.g. capped or away),
                # pick the first candidate whose id > last_id, else wrap around to 0
                higher_candidates = [idx for idx, u in enumerate(candidate_users) if u.id > last_id]
                next_idx = higher_candidates[0] if higher_candidates else 0

        chosen_user = candidate_users[next_idx]
        chosen_load = workload_counts.get(chosen_user.id, 0)
        # Update organization pointer
        org.last_assigned_user = chosen_user
        org.save(update_fields=["last_assigned_user"])

    if not chosen_user:
        return None

    # Apply assignment
    conversation.assigned_to = chosen_user
    conversation.save(update_fields=["assigned_to"])

    chosen_user.last_lead_assigned_at = timezone.now()
    chosen_user.save(update_fields=["last_lead_assigned_at"])

    rep_name = chosen_user.first_name or chosen_user.email
    logger.info(
        "Assigned conversation #%d to %s via %s (Active load: %d)",
        conversation.id,
        chosen_user.email,
        strat_label,
        chosen_load,
    )

    sys_msg = Message.objects.create(
        conversation=conversation,
        organization=org,
        sender_type=Message.SenderType.SYSTEM,
        direction=Message.Direction.OUTBOUND,
        status=Message.Status.DELIVERED,
        body=f"Lead automatically assigned to {rep_name} via {strat_label} ({chosen_load} active chats).",
    )

    # Real-time WebSocket lead assignment notification
    try:
        from messaging.ws_broadcast import broadcast_user_event, broadcast_chat_event, broadcast_inbox_event
        contact = conversation.contact
        contact_name = (contact.first_name + (" " + contact.last_name if contact.last_name else "")).strip() if (contact.first_name or contact.last_name) else contact.phone
        phone_display = f" ({contact.phone})" if contact.phone and contact_name != contact.phone else ""
        lead_summary = f"{contact_name}{phone_display}"

        broadcast_user_event(
            chosen_user.id,
            {
                "event": "inbox_update",
                "conversation": {
                    "id": conversation.id,
                    "last_body": sys_msg.body,
                    "direction": sys_msg.direction,
                    "last_message_at_display": "just now",
                    "assigned_to_id": chosen_user.id,
                },
                "toast": {
                    "type": "lead_assigned",
                    "title": "🎯 New Qualified Lead Assigned",
                    "body": lead_summary,
                    "conversation_id": conversation.id,
                    "contact_name": contact_name,
                    "contact_phone": contact.phone,
                    "assigned_to_id": chosen_user.id,
                    "assigned_to_name": chosen_user.get_full_name() or chosen_user.email,
                    "notification_sound": getattr(conversation.organization, "notification_sound", "sales_flourish"),
                    "custom_sound_url": conversation.organization.custom_notification_sound.url if (conversation.organization and conversation.organization.custom_notification_sound) else None,
                },
            },
        )
        broadcast_inbox_event(
            conversation.organization_id,
            {
                "event": "conversation_assigned",
                "conversation_id": conversation.id,
                "assigned_to_id": chosen_user.id,
                "assigned_to_name": chosen_user.get_full_name() or chosen_user.email,
            },
        )
        broadcast_chat_event(
            conversation.id,
            {
                "event": "new_message",
                "message": {
                    "id": sys_msg.id,
                    "body": sys_msg.body,
                    "direction": sys_msg.direction,
                    "sender_type": sys_msg.sender_type,
                    "created_at": sys_msg.created_at.strftime("%I:%M %p"),
                    "status": sys_msg.status,
                },
            },
        )
    except Exception as exc:
        logger.warning("Failed to broadcast lead assignment alert for user %s: %s", chosen_user.id, exc)

    return chosen_user

