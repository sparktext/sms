@login_required
def inbox_live_feed(request):
    """
    Lightweight polling endpoint: returns recent conversation activity for the
    left-sidebar list. The browser polls this every 5 seconds and updates
    conversation cards without a full page reload.

    Returns up to 30 conversations updated in the last 60 seconds as JSON,
    ordered newest-first.

    Response shape:
    {
      "conversations": [
        {
          "id": 42,
          "contact_name": "Alice Smith",
          "contact_initials": "A",
          "last_body": "Hey, any updates?",
          "direction": "inbound",
          "last_message_at_epoch": 1234567890,
          "last_message_at_display": "2 minutes ago",
          "unread_count": 3
        },
        ...
      ]
    }
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"conversations": []})

    since = timezone.now() - timedelta(seconds=60)
    convs = (
        Conversation.objects
        .filter(organization=org, last_message_at__gte=since)
        .select_related("contact")
        .order_by("-last_message_at")[:30]
    )

    result = []
    for c in convs:
        contact = c.contact
        first = (contact.first_name or "").strip()
        last = (contact.last_name or "").strip()
        contact_name = f"{first} {last}".strip() or contact.phone

        # Initials for avatar
        initials = (first[:1] or last[:1] or "#").upper()

        # Last message snippet (avoid extra query by checking cached_last_msg)
        last_msg = getattr(c, "cached_last_msg", None)
        if last_msg is None:
            last_msg = c.messages.order_by("-created_at").first()

        last_body = ""
        direction = ""
        if last_msg:
            last_body = last_msg.body or ""
            direction = last_msg.direction or ""

        result.append({
            "id": c.id,
            "contact_name": contact_name,
            "contact_initials": initials,
            "last_body": last_body[:120],
            "direction": direction,
            "last_message_at_epoch": int(c.last_message_at.timestamp()) if c.last_message_at else 0,
            "last_message_at_display": _timesince_short(c.last_message_at),
            "unread_count": c.unread_count or 0,
        })

    return JsonResponse({"conversations": result})


def _timesince_short(dt):
    """Returns a compact timesince string like '2m ago' or 'just now'."""
    if not dt:
        return ""
    delta = timezone.now() - dt
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return "just now"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m ago"
    if total_seconds < 86400:
        return f"{total_seconds // 3600}h ago"
    return f"{total_seconds // 86400}d ago"

