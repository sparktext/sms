from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import Resolver404, resolve

# Paths that every host may serve as-is: health checks (Docker/CD probe via 127.0.0.1 too),
# CSS/JS/images used by both sites, and uploaded media.
_ALWAYS_ALLOWED_PREFIXES = ("/healthz", "/readyz", "/static/", "/media/", "/favicon.ico")


class _PermanentRedirect(HttpResponseRedirect):
    status_code = 301


class _PermanentRedirectKeepMethod(HttpResponseRedirect):
    # 308 = like 301, but the client must repeat the SAME method + body (a POST stays a POST).
    status_code = 308


class HostRoutingMiddleware:
    """
    One Django project, two public hostnames:

        MARKETING_HOST (sparktext.si)      -> only the `website` app (home, about, pricing, ...)
        APP_HOST       (app.sparktext.si)  -> everything else (login, dashboard, API, webhooks)

    A request on the "wrong" host is redirected to the same path on the right one, so old links,
    bookmarks and relative links in templates (e.g. "/terms/" in the signup page) keep working.
    Requests for any other host (localhost, 127.0.0.1) are untouched, and the middleware is a
    no-op when either setting is empty (local dev, CI).
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.marketing_host = settings.MARKETING_HOST.lower()
        self.app_host = settings.APP_HOST.lower()

    def __call__(self, request):
        redirect_to = self._redirect_target(request) if self.marketing_host and self.app_host else None
        if redirect_to is None:
            return self.get_response(request)
        if request.method in ("GET", "HEAD"):
            return _PermanentRedirect(redirect_to)
        return _PermanentRedirectKeepMethod(redirect_to)

    def _redirect_target(self, request):
        host = request.get_host().split(":")[0].lower()
        if host not in (self.marketing_host, self.app_host):
            return None
        if request.path_info.startswith(_ALWAYS_ALLOWED_PREFIXES):
            return None

        try:
            is_marketing_page = resolve(request.path_info).namespace == "website"
        except Resolver404:
            return None  # let Django answer (404, or APPEND_SLASH adding the "/")

        if host == self.marketing_host and not is_marketing_page:
            return f"https://{self.app_host}{request.get_full_path()}"
        if host == self.app_host and is_marketing_page:
            if request.path_info == "/":
                # The app's front door: dashboard when signed in, otherwise the login page.
                return "/app/"
            return f"https://{self.marketing_host}{request.get_full_path()}"
        return None
