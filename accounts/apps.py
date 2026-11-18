
from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounts'

    def ready(self) -> None:
        import accounts.signals  # noqa: F401 — register signal handlers
        self._sync_site_domain()

    def _sync_site_domain(self) -> None:
        """
        Keep the django.contrib.sites domain in sync with PUBLIC_BASE_URL from
        settings so that allauth verification emails always link to the right host.

        Dev   → PUBLIC_BASE_URL=http://localhost:8000  → domain=localhost:8000
        Prod  → PUBLIC_BASE_URL=https://sparktext.si   → domain=sparktext.si
        """
        from django.conf import settings
        from urllib.parse import urlparse

        base_url = getattr(settings, "PUBLIC_BASE_URL", "")
        if not base_url:
            return

        parsed = urlparse(base_url)
        domain = parsed.netloc  # e.g. "localhost:8000" or "sparktext.si"
        name = "SparkText Dev" if settings.DEBUG else "SparkText"
        site_id = getattr(settings, "SITE_ID", 1)

        try:
            from django.contrib.sites.models import Site
            Site.objects.filter(id=site_id).update(domain=domain, name=name)
        except Exception:
            # DB not yet ready (before first migrate) — silently skip
            pass

