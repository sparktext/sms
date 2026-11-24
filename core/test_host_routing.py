from django.test import TestCase, override_settings

from core.middleware import HostRoutingMiddleware

MARKETING = "sparktext.si"
APP = "app.sparktext.si"


@override_settings(
    MARKETING_HOST=MARKETING,
    APP_HOST=APP,
    ALLOWED_HOSTS=[MARKETING, APP, "127.0.0.1", "testserver"],
)
class HostRoutingTests(TestCase):
    # --- sparktext.si: landing pages only ---

    def test_marketing_host_serves_landing_pages(self):
        for path in ("/", "/about/", "/pricing/", "/contact/", "/terms/", "/privacy/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, HTTP_HOST=MARKETING).status_code, 200)

    def test_marketing_host_sends_app_urls_to_app_host_keeping_query(self):
        resp = self.client.get("/accounts/login/?next=/app/", HTTP_HOST=MARKETING)
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp["Location"], "https://app.sparktext.si/accounts/login/?next=/app/")

    def test_marketing_host_sends_admin_to_app_host(self):
        resp = self.client.get("/admin/", HTTP_HOST=MARKETING)
        self.assertEqual(resp["Location"], "https://app.sparktext.si/admin/")

    def test_post_redirect_keeps_method(self):
        # A webhook mistakenly pointed at the bare domain must not turn into a GET (and lose its body).
        resp = self.client.post("/api/v1/leads/", {"x": "1"}, HTTP_HOST=MARKETING)
        self.assertEqual(resp.status_code, 308)
        self.assertEqual(resp["Location"], "https://app.sparktext.si/api/v1/leads/")

    # --- app.sparktext.si: everything except the landing pages ---

    def test_app_host_root_goes_to_app_front_door(self):
        resp = self.client.get("/", HTTP_HOST=APP)
        self.assertEqual(resp["Location"], "/app/")

    def test_app_host_sends_landing_pages_to_marketing_host(self):
        # e.g. the "Terms" link in the signup page footer
        resp = self.client.get("/terms/", HTTP_HOST=APP)
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp["Location"], "https://sparktext.si/terms/")

    def test_app_host_serves_app_pages(self):
        self.assertEqual(self.client.get("/accounts/login/", HTTP_HOST=APP).status_code, 200)

    # --- shared / untouched ---

    def test_health_checks_work_on_every_host(self):
        for host in (MARKETING, APP, "127.0.0.1"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/healthz", HTTP_HOST=host).status_code, 200)

    def test_unknown_host_is_not_redirected(self):
        # Docker healthcheck / local access by IP
        self.assertEqual(self.client.get("/", HTTP_HOST="127.0.0.1").status_code, 200)

    def test_unknown_path_is_a_404_not_a_redirect(self):
        self.assertEqual(self.client.get("/no-such-page/", HTTP_HOST=MARKETING).status_code, 404)


@override_settings(MARKETING_HOST="", APP_HOST="")
class HostRoutingDisabledTests(TestCase):
    def test_noop_when_hosts_not_configured(self):
        # Local dev and CI: one host serves everything, exactly as before.
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/accounts/login/").status_code, 200)

    def test_middleware_reads_settings_at_startup(self):
        mw = HostRoutingMiddleware(lambda r: None)
        self.assertEqual((mw.marketing_host, mw.app_host), ("", ""))
