from django.contrib.auth import login
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.text import slugify

from .forms import SignupForm
from .models import Organization, User


def unique_slug(name):
    base = slugify(name)[:40] or "org"
    slug, n = base, 1
    while Organization.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


def signup(request):
    if request.user.is_authenticated:
        return redirect("messaging:dashboard")

    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        with transaction.atomic():  # company and user are created together or not at all
            org = Organization.objects.create(
                name=d["company_name"], slug=unique_slug(d["company_name"])
            )
            user = User.objects.create_user(
                email=d["email"],
                password=d["password1"],  # raw password: the manager hashes it
                first_name=d["first_name"],
                last_name=d["last_name"],
                organization=org,
                role=User.Role.OWNER,
                terms_accepted_at=timezone.now(),
            )
        login(request, user)
        return redirect("messaging:dashboard")
    return render(request, "core/signup.html", {"form": form})

