import csv
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET

from .models import ConsentEvent
from .services import record_consent, classify_keyword
from contacts.models import Contact
from contacts.utils import normalize_phone


@login_required
def compliance_dashboard(request):
    """
    Main compliance dashboard:
    - Lists append-only consent events
    - Metrics for TCPA compliance (opt-ins, opt-outs, audit events)
    - Filter by event type, source, contact phone/name
    - Keyword testing sandbox & modal for recording consent
    """
    org = request.user.organization
    if not org:
        return render(request, "contacts/no_organization.html")

    events_qs = (
        ConsentEvent.objects.filter(organization=org)
        .select_related("contact")
        .order_by("-created_at")
    )

    # Search query
    q = request.GET.get("q", "").strip()
    if q:
        events_qs = events_qs.filter(
            Q(contact__phone__icontains=q)
            | Q(contact__first_name__icontains=q)
            | Q(contact__last_name__icontains=q)
            | Q(proof_text__icontains=q)
        )

    # Event type filter
    event_type = request.GET.get("event_type", "").strip()
    if event_type in ConsentEvent.EventType.values:
        events_qs = events_qs.filter(event_type=event_type)

    # Source filter
    source = request.GET.get("source", "").strip()
    if source in ConsentEvent.Source.values:
        events_qs = events_qs.filter(source=source)

    # Metrics
    total_events = ConsentEvent.objects.filter(organization=org).count()
    opt_in_events = ConsentEvent.objects.filter(
        organization=org, event_type=ConsentEvent.EventType.OPT_IN
    ).count()
    opt_out_events = ConsentEvent.objects.filter(
        organization=org, event_type=ConsentEvent.EventType.OPT_OUT
    ).count()

    active_opted_in = Contact.objects.filter(
        organization=org, sms_status=Contact.SMSStatus.OPTED_IN
    ).count()
    active_opted_out = Contact.objects.filter(
        organization=org, sms_status=Contact.SMSStatus.OPTED_OUT
    ).count()

    paginator = Paginator(events_qs, 25)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    # Pre-fetch recent contacts for the manual record consent dropdown
    recent_contacts = Contact.objects.filter(organization=org).order_by("-created_at")[:50]

    context = {
        "org": org,
        "page_obj": page_obj,
        "q": q,
        "event_type": event_type,
        "source": source,
        "total_events": total_events,
        "opt_in_events": opt_in_events,
        "opt_out_events": opt_out_events,
        "active_opted_in": active_opted_in,
        "active_opted_out": active_opted_out,
        "recent_contacts": recent_contacts,
        "event_types": ConsentEvent.EventType.choices,
        "sources": ConsentEvent.Source.choices,
    }
    return render(request, "compliance/dashboard.html", context)


@login_required
@require_POST
def record_consent_view(request):
    """
    Manually record consent for a contact (verbal, written form, or manual request).
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    contact_id = request.POST.get("contact_id", "").strip()
    phone = request.POST.get("phone", "").strip()
    event_type = request.POST.get("event_type", "").strip()
    source = request.POST.get("source", ConsentEvent.Source.MANUAL).strip()
    proof_text = request.POST.get("proof_text", "").strip()

    if not contact_id and not phone:
        return JsonResponse(
            {"status": "error", "message": "Please select a contact or enter a valid phone number."},
            status=400,
        )

    contact = None
    if contact_id:
        try:
            contact = Contact.objects.get(id=contact_id, organization=org)
        except (Contact.DoesNotExist, ValueError):
            return JsonResponse({"status": "error", "message": "Selected contact not found."}, status=400)
    elif phone:
        try:
            normalized = normalize_phone(phone)
            contact = Contact.objects.filter(organization=org, phone=normalized).first()
            if not contact:
                return JsonResponse(
                    {"status": "error", "message": f"No contact found for phone number {normalized}."},
                    status=400,
                )
        except ValidationError as ve:
            return JsonResponse({"status": "error", "message": str(ve)}, status=400)

    try:
        event = record_consent(
            contact=contact,
            event_type=event_type,
            source=source,
            proof_text=proof_text,
        )
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
            return JsonResponse({
                "status": "success",
                "event_id": event.id,
                "phone": contact.phone,
                "event_type": event.event_type,
                "created_at": event.created_at.strftime("%b %d, %Y %H:%M"),
            })
        return redirect("compliance:dashboard")
    except ValidationError as ve:
        err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        return JsonResponse({"status": "error", "message": err_msg}, status=400)
    except Exception as exc:
        return JsonResponse({"status": "error", "message": f"Failed to record consent: {str(exc)}"}, status=400)


@login_required
def test_keyword_view(request):
    """
    Live test tool for classifying SMS keywords.
    """
    text = request.GET.get("text", "") or request.POST.get("text", "")
    classification = classify_keyword(text)

    explanation = "No standard compliance keyword detected."
    if classification == "STOP":
        explanation = "CTIA/TCPA Mandatory STOP keyword. Subscriber will be immediately marked as Opted Out and blocked from further marketing."
    elif classification == "START":
        explanation = "CTIA/TCPA START / UNSTOP keyword. Subscriber will be opted in and permission granted to resume messaging."
    elif classification == "HELP":
        explanation = "CTIA Mandatory HELP keyword. Standard help/information auto-response required."
    elif text and text.strip().upper() == "YES":
        explanation = "'YES' is intentionally NOT treated as START because conversational AI and qualification flows ask yes/no questions."

    return JsonResponse({
        "status": "success",
        "input": text,
        "classification": classification,
        "explanation": explanation,
    })


@login_required
@require_GET
def export_ledger_csv(request):
    """
    Download complete append-only audit trail as CSV for legal / carrier compliance audits.
    """
    org = request.user.organization
    if not org:
        return HttpResponse("No organization", status=400)

    events = (
        ConsentEvent.objects.filter(organization=org)
        .select_related("contact")
        .order_by("-created_at")
    )

    response = HttpResponse(content_type="text/csv")
    filename = f"sparktext_compliance_audit_{timezone.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        "Event ID",
        "Timestamp (UTC)",
        "Phone",
        "Contact Name",
        "Event Type",
        "Source",
        "Proof Evidence",
    ])

    for ev in events:
        writer.writerow([
            ev.id,
            ev.created_at.isoformat(),
            ev.contact.phone,
            f"{ev.contact.first_name} {ev.contact.last_name}".strip(),
            ev.get_event_type_display(),
            ev.get_source_display(),
            ev.proof_text,
        ])

    return response
