"""
accounts/middleware.py
----------------------
InactivityLogoutMiddleware: automatically logs out users who have been idle
for more than INACTIVITY_TIMEOUT seconds (default: 1800 = 30 minutes).
"""
from __future__ import annotations

import datetime

from django.conf import settings
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.utils import timezone


class InactivityLogoutMiddleware:
    """
    Tracks the last request timestamp in the session.
    If the user has been idle longer than INACTIVITY_TIMEOUT, they are
    logged out and redirected to the login page with reason=inactivity.
    """

    @property
    def TIMEOUT(self) -> int:
        # Read on every request (it used to be read once at startup, so a changed setting was ignored)
        return int(getattr(settings, "INACTIVITY_TIMEOUT", 1800))

    def __init__(self, get_response) -> None:
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            last_activity = request.session.get("last_activity")
            now = timezone.now().timestamp()

            if last_activity is not None:
                elapsed = now - last_activity
                if elapsed > self.TIMEOUT:
                    logout(request)
                    login_url = getattr(settings, "LOGIN_URL", "/accounts/login/")
                    return redirect(f"{login_url}?reason=inactivity")

            # Refresh the timestamp on every request
            request.session["last_activity"] = now
            request.session.modified = True

        return self.get_response(request)

