from django.db.models import Sum


def unread_inbox_count(request):
    """
    Supplies total unread conversations count for the current user's organization
    to all templates (used for the nav sidebar badge).
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {"nav_unread_count": 0}

    org = getattr(request.user, "organization", None)
    if not org:
        return {"nav_unread_count": 0}

    from .models import Conversation

    total = (
        Conversation.objects.filter(
            organization=org,
            status__in=[Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN],
        ).aggregate(total=Sum("unread_count"))["total"]
        or 0
    )
    return {"nav_unread_count": total}

