from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_http_methods
from django.http import JsonResponse, HttpResponseForbidden
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import login
from django.db import transaction
from django.db.models import Count

from .models import Organization, User, OrganizationInvite

US_TIMEZONES = [
    ("America/New_York", "Eastern Time (US & Canada) — UTC-5 / UTC-4"),
    ("America/Chicago", "Central Time (US & Canada) — UTC-6 / UTC-5"),
    ("America/Denver", "Mountain Time (US & Canada) — UTC-7 / UTC-6"),
    ("America/Phoenix", "Mountain Time (Arizona - No DST) — UTC-7"),
    ("America/Los_Angeles", "Pacific Time (US & Canada) — UTC-8 / UTC-7"),
    ("America/Anchorage", "Alaska — UTC-9 / UTC-8"),
    ("Pacific/Honolulu", "Hawaii — UTC-10"),
    ("UTC", "UTC (Coordinated Universal Time)"),
]


def _is_ajax(request) -> bool:
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    return False


@login_required
def settings_redirect(request):
    """Redirect top-level /settings/ to company profile."""
    return redirect("company_profile")


# =========================================================================
# COMPANY PROFILE
# =========================================================================

@login_required
@require_http_methods(["GET", "POST"])
def company_profile(request):
    org = request.user.organization
    if not org:
        messages.error(request, "No organization associated with this account.")
        return redirect("messaging:dashboard")

    is_admin = request.user.role in [User.Role.OWNER, User.Role.ADMIN]

    if request.method == "POST":
        if not is_admin:
            messages.error(request, "Only Owners and Admins can update Company Profile settings.")
            return redirect("company_profile")

        name = request.POST.get("name", "").strip()
        ein = request.POST.get("ein", "").strip()
        website = request.POST.get("website", "").strip()
        support_phone = request.POST.get("support_phone", "").strip()
        notification_email = request.POST.get("notification_email", "").strip()
        tz_val = request.POST.get("timezone", "").strip()
        address_street = request.POST.get("address_street", "").strip()
        address_city = request.POST.get("address_city", "").strip()
        address_state = request.POST.get("address_state", "").strip()
        address_postal_code = request.POST.get("address_postal_code", "").strip()
        address_country = request.POST.get("address_country", "USA").strip()

        if not name:
            messages.error(request, "Company name cannot be empty.")
            return redirect("company_profile")

        org.name = name
        org.ein = ein
        org.website = website
        org.support_phone = support_phone
        org.notification_email = notification_email
        if tz_val:
            org.timezone = tz_val
        org.address_street = address_street
        org.address_city = address_city
        org.address_state = address_state
        org.address_postal_code = address_postal_code
        org.address_country = address_country or "USA"
        org.save()

        messages.success(request, "Company Profile updated successfully.")
        return redirect("company_profile")

    context = {
        "org": org,
        "is_admin": is_admin,
        "timezones": US_TIMEZONES,
        "current_tab": "profile",
    }
    return render(request, "core/company_profile.html", context)


# =========================================================================
# TEAM MEMBERS & PERMISSIONS
# =========================================================================

@login_required
def team_members(request):
    org = request.user.organization
    if not org:
        messages.error(request, "No organization associated with this account.")
        return redirect("messaging:dashboard")

    is_admin = request.user.role in [User.Role.OWNER, User.Role.ADMIN]

    from messaging.models import Conversation
    active_statuses = [Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN]

    members = list(User.objects.filter(organization=org, is_active=True).order_by("first_name", "email"))
    workload_map = dict(
        Conversation.objects.filter(
            organization=org,
            assigned_to__in=members,
            status__in=active_statuses,
        )
        .values("assigned_to_id")
        .annotate(total=Count("id"))
        .values_list("assigned_to_id", "total")
    )

    for m in members:
        m.active_leads_count = workload_map.get(m.id, 0)
        m.is_capped = (org.max_leads_per_agent > 0 and m.active_leads_count >= org.max_leads_per_agent)

    pending_invites = OrganizationInvite.objects.filter(
        organization=org,
        status=OrganizationInvite.Status.PENDING,
        expires_at__gt=timezone.now(),
    ).order_by("-created_at")

    owner_count = sum(1 for m in members if m.role == User.Role.OWNER)

    context = {
        "org": org,
        "is_admin": is_admin,
        "members": members,
        "pending_invites": pending_invites,
        "owner_count": owner_count,
        "role_choices": User.Role.choices,
        "strategies": Organization.LeadRoutingStrategy.choices,
        "current_tab": "team",
    }
    return render(request, "core/team_members.html", context)


@login_required
@require_POST
def team_invite_create(request):
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    if request.user.role not in [User.Role.OWNER, User.Role.ADMIN]:
        return JsonResponse({"status": "error", "message": "Only Owners and Admins can invite team members."}, status=403)

    email = request.POST.get("email", "").strip().lower()
    role = request.POST.get("role", User.Role.AGENT).strip().lower()
    if role in ["viewer", "read_only", "readonly"]:
        role = User.Role.READONLY

    if not email:
        return JsonResponse({"status": "error", "message": "Please enter a valid email address."}, status=400)

    valid_roles = [r[0] for r in User.Role.choices]
    if role not in valid_roles:
        role = User.Role.AGENT

    # Check if user is already an active member of this organization
    if User.objects.filter(organization=org, email=email, is_active=True).exists():
        return JsonResponse({"status": "error", "message": f"{email} is already a team member."}, status=400)

    # Invalidate any previous pending invites for this email in this org
    OrganizationInvite.objects.filter(
        organization=org,
        email=email,
        status=OrganizationInvite.Status.PENDING,
    ).update(status=OrganizationInvite.Status.REVOKED)

    invite = OrganizationInvite.objects.create(
        organization=org,
        email=email,
        role=role,
        invited_by=request.user,
    )

    invite_url = request.build_absolute_uri(reverse("accept_invite", kwargs={"token": invite.token}))

    if _is_ajax(request):
        return JsonResponse({
            "status": "success",
            "message": f"Invitation created for {email}.",
            "invite": {
                "id": invite.id,
                "email": invite.email,
                "role": invite.role,
                "role_display": invite.get_role_display(),
                "created_at": invite.created_at.strftime("%b %d, %Y"),
                "invite_url": invite_url,
            }
        }, status=201)

    messages.success(request, f"Invitation created for {email}. Share link: {invite_url}")
    return redirect("team_members")


@login_required
@require_POST
def team_invite_revoke(request, invite_id):
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    if request.user.role not in [User.Role.OWNER, User.Role.ADMIN]:
        return JsonResponse({"status": "error", "message": "Permission denied."}, status=403)

    invite = get_object_or_404(OrganizationInvite, id=invite_id, organization=org)
    invite.status = OrganizationInvite.Status.REVOKED
    invite.save(update_fields=["status"])

    if _is_ajax(request):
        return JsonResponse({"status": "success", "message": f"Invitation for {invite.email} revoked."})

    messages.success(request, f"Invitation for {invite.email} revoked.")
    return redirect("team_members")


@login_required
@require_POST
def team_member_update_role(request, user_id):
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    if request.user.role not in [User.Role.OWNER, User.Role.ADMIN]:
        return JsonResponse({"status": "error", "message": "Permission denied."}, status=403)

    target_user = get_object_or_404(User, id=user_id, organization=org)
    new_role = request.POST.get("role", "").strip().lower()
    if new_role in ["viewer", "read_only", "readonly"]:
        new_role = User.Role.READONLY

    valid_roles = [r[0] for r in User.Role.choices]
    if new_role not in valid_roles:
        return JsonResponse({"status": "error", "message": "Invalid role."}, status=400)

    # Protect sole Owner from being demoted
    if target_user.role == User.Role.OWNER and new_role != User.Role.OWNER:
        owner_count = User.objects.filter(organization=org, role=User.Role.OWNER, is_active=True).count()
        if owner_count <= 1:
            msg = "Organization must have at least one active Owner. Assign another Owner first."
            if _is_ajax(request):
                return JsonResponse({"status": "error", "message": msg}, status=400)
            messages.error(request, msg)
            return redirect("team_members")

    # Only Owners can grant Owner role
    if new_role == User.Role.OWNER and request.user.role != User.Role.OWNER:
        msg = "Only existing Owners can promote a member to Owner."
        if _is_ajax(request):
            return JsonResponse({"status": "error", "message": msg}, status=403)
        messages.error(request, msg)
        return redirect("team_members")

    target_user.role = new_role
    target_user.save(update_fields=["role"])

    if _is_ajax(request):
        return JsonResponse({
            "status": "success",
            "message": f"Updated {target_user.email} to {target_user.get_role_display()}.",
            "role": target_user.role,
            "role_display": target_user.get_role_display(),
        })

    messages.success(request, f"Updated {target_user.email} to {target_user.get_role_display()}.")
    return redirect("team_members")


@login_required
@require_POST
def team_member_remove(request, user_id):
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    if request.user.role not in [User.Role.OWNER, User.Role.ADMIN]:
        return JsonResponse({"status": "error", "message": "Permission denied."}, status=403)

    target_user = get_object_or_404(User, id=user_id, organization=org)

    # Cannot remove sole Owner
    if target_user.role == User.Role.OWNER:
        owner_count = User.objects.filter(organization=org, role=User.Role.OWNER, is_active=True).count()
        if owner_count <= 1:
            msg = "Cannot remove the only Owner of the organization."
            if _is_ajax(request):
                return JsonResponse({"status": "error", "message": msg}, status=400)
            messages.error(request, msg)
            return redirect("team_members")

    # Admins cannot remove Owners
    if target_user.role == User.Role.OWNER and request.user.role != User.Role.OWNER:
        msg = "Admins cannot remove an Owner."
        if _is_ajax(request):
            return JsonResponse({"status": "error", "message": msg}, status=403)
        messages.error(request, msg)
        return redirect("team_members")

    # Dissociate user
    target_email = target_user.email
    target_user.organization = None
    target_user.is_active = False
    target_user.save(update_fields=["organization", "is_active"])

    if _is_ajax(request):
        return JsonResponse({"status": "success", "message": f"{target_email} removed from organization."})

    messages.success(request, f"{target_email} removed from organization.")
    return redirect("team_members")


# =========================================================================
# PUBLIC INVITE ACCEPTANCE FLOW
# =========================================================================

@require_http_methods(["GET", "POST"])
def accept_invite(request, token):
    invite = get_object_or_404(OrganizationInvite, token=token)

    if not invite.is_valid:
        return render(request, "core/accept_invite_expired.html", {"invite": invite})

    # Clear any stale session messages accumulated from other views
    storage = messages.get_messages(request)
    for _ in storage:
        pass

    error = None
    if request.method == "POST":
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()
        password = request.POST.get("password", "").strip()
        confirm_password = request.POST.get("confirm_password", "").strip()

        if not first_name:
            error = "Please enter your first name."
            return render(request, "core/accept_invite.html", {"invite": invite, "error": error, "first_name": first_name, "last_name": last_name})

        if not password or len(password) < 8:
            error = "Password must be at least 8 characters."
            return render(request, "core/accept_invite.html", {"invite": invite, "error": error, "first_name": first_name, "last_name": last_name})

        if password != confirm_password:
            error = "Passwords do not match."
            return render(request, "core/accept_invite.html", {"invite": invite, "error": error, "first_name": first_name, "last_name": last_name})

        with transaction.atomic():
            # If user already exists with this email, update org and password; otherwise create
            user = User.objects.filter(email=invite.email).first()
            if user:
                user.first_name = first_name
                user.last_name = last_name
                user.set_password(password)
                user.organization = invite.organization
                user.role = invite.role
                user.is_active = True
                user.terms_accepted_at = timezone.now()
                user.save()
            else:
                user = User.objects.create_user(
                    email=invite.email,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                    organization=invite.organization,
                    role=invite.role,
                    terms_accepted_at=timezone.now(),
                )

            invite.status = OrganizationInvite.Status.ACCEPTED
            invite.save(update_fields=["status"])

        login(request, user)
        messages.success(request, f"Welcome to {invite.organization.name}! You are logged in as {invite.get_role_display()}.")
        return redirect("messaging:inbox")

    context = {
        "invite": invite,
    }
    return render(request, "core/accept_invite.html", context)


# =========================================================================
# LEAD DISTRIBUTION & ROUTING SETTINGS
# =========================================================================

@login_required
@require_http_methods(["GET", "POST"])
def lead_routing_settings(request):
    org = request.user.organization
    if not org:
        messages.error(request, "No organization associated with this account.")
        return redirect("messaging:dashboard")

    is_admin = request.user.role in [User.Role.OWNER, User.Role.ADMIN]

    if request.method == "POST":
        if not is_admin:
            messages.error(request, "Only Owners and Admins can update Lead Routing settings.")
            return redirect("team_members")

        strategy = request.POST.get("lead_routing_strategy", "").strip()
        valid_strategies = [s[0] for s in Organization.LeadRoutingStrategy.choices]
        if strategy in valid_strategies:
            org.lead_routing_strategy = strategy

        try:
            max_leads = int(request.POST.get("max_leads_per_agent", "5"))
            org.max_leads_per_agent = max(0, min(max_leads, 100))
        except ValueError:
            pass

        org.route_only_available = True
        org.save(update_fields=["lead_routing_strategy", "max_leads_per_agent", "route_only_available"])

        if _is_ajax(request):
            return JsonResponse({
                "status": "success",
                "message": "Lead routing settings saved successfully.",
            })

        messages.success(request, "Lead routing settings saved successfully.")
        return redirect("team_members")

    return team_members(request)


@login_required
@require_POST
def toggle_agent_availability(request, user_id=None):
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    target_user = request.user
    if user_id:
        if request.user.role not in [User.Role.OWNER, User.Role.ADMIN]:
            return JsonResponse({"status": "error", "message": "Permission denied."}, status=403)
        target_user = get_object_or_404(User, id=user_id, organization=org)

    if target_user.role == User.Role.READONLY:
        return JsonResponse({"status": "error", "message": "Read-only members do not participate in lead routing."}, status=400)

    target_user.is_available_for_leads = not target_user.is_available_for_leads
    target_user.save(update_fields=["is_available_for_leads"])

    new_state = target_user.is_available_for_leads
    label = "Available for Leads" if new_state else "Away / Paused"

    if _is_ajax(request):
        return JsonResponse({
            "status": "success",
            "user_id": target_user.id,
            "is_available": new_state,
            "label": label,
        })

    messages.success(request, f"Status updated: {target_user.first_name or target_user.email} is now {label}.")
    return redirect(request.META.get("HTTP_REFERER") or "lead_routing_settings")


# =========================================================================
# NOTIFICATION SOUNDS & CHIME SETTINGS
# =========================================================================

PRESET_SOUND_METADATA = [
    {
        "id": Organization.NotificationSound.SALES_FLOURISH,
        "name": "Sales Flourish (Ascending Major Chime)",
        "badge": "Recommended for Sales",
        "badge_color": "emerald",
        "description": "Bright 4-note ascending G-major arpeggio (G5 -> B5 -> D6 -> G6) with crystal bell harmonics and vibrant ring-out. High dopamine, ideal for sales reps.",
        "category": "High Energy",
    },
    {
        "id": Organization.NotificationSound.CASH_REGISTER,
        "name": "Cash Register (Cha-Ching)",
        "badge": "Classic Closer",
        "badge_color": "amber",
        "description": "Crisp mechanical roller click followed by a dual high-bell metallic ring. The timeless sound of revenue and closed deals.",
        "category": "Celebratory",
    },
    {
        "id": Organization.NotificationSound.CRYSTAL_BELL,
        "name": "Crystal Bell Ding",
        "badge": "Minimalist",
        "badge_color": "blue",
        "description": "Single pristine luxury glass bell ding (1318 Hz) with long overtone sustain. Clean, elegant, and non-intrusive.",
        "category": "Subtle & Elegant",
    },
    {
        "id": Organization.NotificationSound.BRIGHT_POP,
        "name": "Crisp Pop & Marimba",
        "badge": "Playful",
        "badge_color": "purple",
        "description": "Energetic wooden marimba pop with a bright high-G harmonic bounce. Playful, friendly, and cuts through any background noise.",
        "category": "Distinctive",
    },
    {
        "id": Organization.NotificationSound.CHIME_TRIO,
        "name": "Elevate (Harmonic Trio)",
        "badge": "Modern SaaS",
        "badge_color": "indigo",
        "description": "Upbeat 3-note ascending chord (E5 -> A5 -> C#6) with warm chorus detune. Modern Slack/Linear-style workflow chime.",
        "category": "Corporate",
    },
    {
        "id": Organization.NotificationSound.CLASSIC_RING,
        "name": "Classic Digital Ring",
        "badge": "High Urgency",
        "badge_color": "rose",
        "description": "Double digital pulse (A5 -> D6) with sharp attack. Designed for high urgency when instant lead response is critical.",
        "category": "Urgent",
    },
]


def _broadcast_sound_update(org):
    try:
        from messaging.ws_broadcast import broadcast_inbox_event
        broadcast_inbox_event(
            org.id,
            {
                "event": "notification_sound_update",
                "notification_sound": org.notification_sound,
                "notification_sound_display": org.get_notification_sound_display(),
                "custom_sound_url": org.custom_notification_sound.url if org.custom_notification_sound else None,
            },
        )
    except Exception:
        pass


@login_required
@require_http_methods(["GET", "POST"])
def notification_sound_settings(request):
    org = request.user.organization
    if not org:
        messages.error(request, "No organization associated with this account.")
        return redirect("messaging:dashboard")

    is_admin = request.user.role in [User.Role.OWNER, User.Role.ADMIN]

    if request.method == "POST":
        if not is_admin:
            if _is_ajax(request):
                return JsonResponse({"status": "error", "message": "Only agency Owners and Admins can update notification sounds."}, status=403)
            messages.error(request, "Only agency Owners and Admins can update notification sounds.")
            return redirect("notification_sound_settings")

        action = request.POST.get("action", "save")
        sound = request.POST.get("notification_sound", "").strip()

        # Handle removing custom sound file
        if action == "remove_custom" or request.POST.get("remove_custom_sound") == "true":
            if org.custom_notification_sound:
                org.custom_notification_sound.delete(save=False)
                org.custom_notification_sound = None
            if org.notification_sound == Organization.NotificationSound.CUSTOM:
                org.notification_sound = Organization.NotificationSound.SALES_FLOURISH
            org.save(update_fields=["custom_notification_sound", "notification_sound"])
            _broadcast_sound_update(org)
            if _is_ajax(request):
                return JsonResponse({"status": "success", "message": "Custom sound removed."})
            messages.success(request, "Custom sound removed. Defaulted to Sales Flourish.")
            return redirect("notification_sound_settings")

        # Handle uploading custom sound
        custom_file = request.FILES.get("custom_notification_sound")
        if custom_file:
            ext = custom_file.name.split(".")[-1].lower() if "." in custom_file.name else ""
            allowed_exts = ["mp3", "wav", "ogg", "m4a", "aac", "weba", "flac"]
            if ext not in allowed_exts:
                err_msg = f"Invalid audio format '.{ext}'. Supported formats: MP3, WAV, OGG, M4A, AAC."
                if _is_ajax(request):
                    return JsonResponse({"status": "error", "message": err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect("notification_sound_settings")

            if custom_file.size > 5 * 1024 * 1024:
                err_msg = "Audio file exceeds maximum size of 5MB."
                if _is_ajax(request):
                    return JsonResponse({"status": "error", "message": err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect("notification_sound_settings")

            if org.custom_notification_sound:
                org.custom_notification_sound.delete(save=False)

            org.custom_notification_sound = custom_file
            org.notification_sound = Organization.NotificationSound.CUSTOM
            org.save(update_fields=["custom_notification_sound", "notification_sound"])
            _broadcast_sound_update(org)

            if _is_ajax(request):
                return JsonResponse({
                    "status": "success",
                    "message": "Custom sound uploaded and set as active.",
                    "custom_sound_url": org.custom_notification_sound.url,
                    "notification_sound": org.notification_sound,
                })
            messages.success(request, "Custom audio file uploaded and activated successfully.")
            return redirect("notification_sound_settings")

        # Handle preset sound selection
        valid_choices = [c[0] for c in Organization.NotificationSound.choices]
        if sound in valid_choices:
            if sound == Organization.NotificationSound.CUSTOM and not org.custom_notification_sound:
                org.notification_sound = Organization.NotificationSound.SALES_FLOURISH
            else:
                org.notification_sound = sound
            org.save(update_fields=["notification_sound"])
            _broadcast_sound_update(org)

        if _is_ajax(request):
            return JsonResponse({
                "status": "success",
                "message": "Notification sound updated successfully.",
                "notification_sound": org.notification_sound,
            })

        messages.success(request, "Notification sound updated successfully.")
        return redirect("notification_sound_settings")

    context = {
        "org": org,
        "is_admin": is_admin,
        "current_sound": org.notification_sound,
        "custom_sound_url": org.custom_notification_sound.url if org.custom_notification_sound else None,
        "preset_sounds": PRESET_SOUND_METADATA,
        "current_tab": "sounds",
    }
    return render(request, "core/notification_sounds.html", context)
