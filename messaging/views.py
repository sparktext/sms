from django.contrib.auth.decorators import login_required
from django.shortcuts import render


def _setup_steps(org):
    """Checklist shown on the dashboard. Fields that don't exist yet count as not done."""
    reg_status = getattr(org, "registration_status", "")
    return [
        {"title": "Create your account", "desc": "Your company and login are ready.", "done": True, "url": ""},
        {"title": "Add business details", "desc": "Legal name, EIN, address and website. Carriers check these.", "done": bool(getattr(org, "ein", "")), "url": "#"},
        {"title": "Register with carriers", "desc": "Submit your brand and campaign so your texts can be delivered.", "done": reg_status == "approved", "url": "#"},
        {"title": "Import your contacts", "desc": "Upload a CSV and record how each person agreed to receive texts.", "done": False, "url": "#"},
        {"title": "Set up the AI Assistant", "desc": "Tell it about your business so it can answer customers.", "done": False, "url": "#"},
        {"title": "Send a test message", "desc": "Text your own phone to see the full flow.", "done": False, "url": "#"},
    ]


@login_required
def dashboard(request):
    org = request.user.organization
    steps = _setup_steps(org)
    done = sum(1 for s in steps if s["done"])

    # Replace these zeros with real counts once the contacts and messaging models exist.
    stats = {"sent_today": 0, "replies": 0, "needs_human": 0, "opt_outs": 0}

    context = {
        "steps": steps,
        "steps_done": done,
        "steps_total": len(steps),
        "percent": int(done / len(steps) * 100),
        "setup_complete": done == len(steps),
        "stats": stats,
    }
    return render(request, "messaging/dashboard.html", context)