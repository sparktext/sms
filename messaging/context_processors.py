import json
import re

from django.db.models import Q, Sum


def unread_inbox_count(request):
    """
    Supplies total unread conversations count for the current user's organization
    to all templates (used for the nav sidebar badge) and initial active needs_human
    notifications targeted to this user (assigned to user or unassigned).
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {"nav_unread_count": 0, "initial_notifications_json": "[]"}

    org = getattr(request.user, "organization", None)
    if not org:
        return {"nav_unread_count": 0, "initial_notifications_json": "[]"}

    from .models import Conversation

    total = (
        Conversation.objects.filter(
            organization=org,
            status__in=[Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN],
        ).aggregate(total=Sum("unread_count"))["total"]
        or 0
    )

    # Detect if user is currently viewing a specific conversation in the inbox
    active_path_match = re.search(r"^/inbox/(\d+)/?", getattr(request, "path", "") or "")
    active_path_conv_id = int(active_path_match.group(1)) if active_path_match else None

    # Active needs_human conversations relevant to this user (unassigned or assigned to this user)
    needs_human_convs = list(
        Conversation.objects.filter(
            organization=org,
            status=Conversation.Status.NEEDS_HUMAN,
        )
        .filter(Q(assigned_to__isnull=True) | Q(assigned_to=request.user))
        .select_related("contact", "assigned_to")
        .order_by("-last_message_at")[:10]
    )

    server_notifs = []
    for c in needs_human_convs:
        contact = c.contact
        c_name = (contact.first_name + (" " + contact.last_name if contact.last_name else "")).strip() if (contact.first_name or contact.last_name) else contact.phone
        reason = (c.handoff_reason or "").strip() or "Customer requires human agent assistance"
        if len(reason) > 120:
            reason = reason[:117] + "..."
        is_currently_viewing = bool(active_path_conv_id and c.id == active_path_conv_id)
        server_notifs.append({
            "id": f"srv-needs-human-{c.id}",
            "type": "needs_human",
            "title": "Needs Human Attention",
            "body": f"{c_name}: {reason}",
            "conversation_id": c.id,
            "url": f"/inbox/{c.id}/",
            "assigned_to_id": c.assigned_to_id,
            "assigned_to_name": (c.assigned_to.get_full_name() or c.assigned_to.email) if c.assigned_to else None,
            "is_assigned_to_me": c.assigned_to_id == request.user.id,
            "timestamp": int(c.last_message_at.timestamp() * 1000) if c.last_message_at else 0,
            "read": is_currently_viewing,
        })

    return {
        "nav_unread_count": total,
        "initial_notifications_json": json.dumps(server_notifs),
    }

