import json
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST, require_GET, require_http_methods

from .models import MessageTemplate
from .defaults import ensure_default_templates


def _is_ajax(request) -> bool:
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    return False


@login_required
def templates_index(request):
    org = request.user.organization
    if not org:
        messages.error(request, "No organization associated with this account.")
        return redirect("messaging:dashboard")

    # Ensure default starter templates exist
    ensure_default_templates(org)

    templates_qs = MessageTemplate.objects.filter(organization=org)

    # Counts by category
    total_count = templates_qs.count()
    marketing_count = templates_qs.filter(category=MessageTemplate.Category.MARKETING).count()
    quick_reply_count = templates_qs.filter(category=MessageTemplate.Category.QUICK_REPLY).count()
    follow_up_count = templates_qs.filter(category=MessageTemplate.Category.FOLLOW_UP).count()
    transactional_count = templates_qs.filter(category=MessageTemplate.Category.TRANSACTIONAL).count()

    # Category filter
    selected_category = request.GET.get("category", "").strip()
    if selected_category and selected_category != "all":
        templates_qs = templates_qs.filter(category=selected_category)

    # Search filter
    q = request.GET.get("q", "").strip()
    if q:
        templates_qs = templates_qs.filter(
            Q(name__icontains=q) | Q(body__icontains=q)
        )

    context = {
        "org": org,
        "templates": templates_qs,
        "selected_category": selected_category or "all",
        "q": q,
        "counts": {
            "all": total_count,
            "marketing": marketing_count,
            "quick_reply": quick_reply_count,
            "follow_up": follow_up_count,
            "transactional": transactional_count,
        },
        "category_choices": MessageTemplate.Category.choices,
    }
    return render(request, "templates_app/index.html", context)


@login_required
@require_POST
def template_create(request):
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    name = request.POST.get("name", "").strip()
    category = request.POST.get("category", MessageTemplate.Category.MARKETING).strip()
    body = request.POST.get("body", "").strip()

    if not name:
        return JsonResponse({"status": "error", "message": "Template name is required."}, status=400)
    if not body:
        return JsonResponse({"status": "error", "message": "Template message body is required."}, status=400)

    valid_cats = [c[0] for c in MessageTemplate.Category.choices]
    if category not in valid_cats:
        category = MessageTemplate.Category.MARKETING

    template = MessageTemplate.objects.create(
        organization=org,
        name=name,
        category=category,
        body=body,
        created_by=request.user,
    )

    if _is_ajax(request):
        return JsonResponse({
            "status": "success",
            "message": f"Template '{template.name}' created.",
            "template": {
                "id": template.id,
                "name": template.name,
                "category": template.category,
                "body": template.body,
                "char_count": template.character_count,
                "segment_count": template.segment_count,
            }
        }, status=201)

    messages.success(request, f"Template '{template.name}' created.")
    return redirect("templates:index")


@login_required
@require_http_methods(["GET", "POST"])
def template_edit(request, template_id):
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    template = get_object_or_404(MessageTemplate, id=template_id, organization=org)

    if request.method == "GET":
        return JsonResponse({
            "status": "success",
            "template": {
                "id": template.id,
                "name": template.name,
                "category": template.category,
                "body": template.body,
                "char_count": template.character_count,
                "segment_count": template.segment_count,
            }
        })

    name = request.POST.get("name", "").strip()
    category = request.POST.get("category", template.category).strip()
    body = request.POST.get("body", "").strip()

    if not name:
        return JsonResponse({"status": "error", "message": "Template name is required."}, status=400)
    if not body:
        return JsonResponse({"status": "error", "message": "Template message body is required."}, status=400)

    valid_cats = [c[0] for c in MessageTemplate.Category.choices]
    if category not in valid_cats:
        category = MessageTemplate.Category.MARKETING

    template.name = name
    template.category = category
    template.body = body
    template.save()

    if _is_ajax(request):
        return JsonResponse({
            "status": "success",
            "message": f"Template '{template.name}' updated.",
            "template": {
                "id": template.id,
                "name": template.name,
                "category": template.category,
                "body": template.body,
                "char_count": template.character_count,
                "segment_count": template.segment_count,
            }
        })

    messages.success(request, f"Template '{template.name}' updated.")
    return redirect("templates:index")


@login_required
@require_POST
def template_delete(request, template_id):
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    template = get_object_or_404(MessageTemplate, id=template_id, organization=org)
    name = template.name
    template.delete()

    if _is_ajax(request):
        return JsonResponse({"status": "success", "message": f"Template '{name}' deleted."})

    messages.success(request, f"Template '{name}' deleted.")
    return redirect("templates:index")


@login_required
@require_GET
def templates_api_list(request):
    """
    JSON API for loading templates in Campaign Creator or Inbox Quick Replies.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    ensure_default_templates(org)

    category = request.GET.get("category", "").strip()
    qs = MessageTemplate.objects.filter(organization=org)
    if category:
        qs = qs.filter(category=category)

    data = [
        {
            "id": t.id,
            "name": t.name,
            "category": t.category,
            "category_label": t.get_category_display(),
            "body": t.body,
            "character_count": t.character_count,
            "segment_count": t.segment_count,
            "merge_tags": t.merge_tags,
        }
        for t in qs
    ]
    return JsonResponse({"status": "success", "templates": data})
