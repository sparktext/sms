"""
Local test mode: TEST_IGNORE_TIME_LIMITS=1 in .env turns OFF every time limit, so calls and campaigns can be tried at any
hour: team call hours and days, closed days / holidays, the legal 8 AM-9 PM calling window, and campaign texting hours.

It only works with DEBUG=True and never under the test suite (sparktext/settings.py), so it can't be switched on in
production, where these limits are the law (TCPA) for real leads. Every page shows a banner while it's on.
"""
from django.conf import settings


def time_limits_off() -> bool:
    return bool(getattr(settings, "TEST_IGNORE_TIME_LIMITS", False))
