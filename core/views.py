from django.shortcuts import redirect


def signup(request):
    """Legacy signup view — redirect to allauth's signup page."""
    return redirect("/accounts/signup/")

