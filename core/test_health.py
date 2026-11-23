from unittest.mock import patch

from django.test import TestCase


class HealthEndpointTests(TestCase):
    """The CD pipeline trusts these endpoints to decide deploy vs. rollback, so they need tests."""

    def test_healthz_returns_ok_without_dependencies(self):
        # Liveness must answer even if Redis is down.
        with patch("core.health._check_redis", side_effect=ConnectionError):
            resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_healthz_is_not_cached(self):
        resp = self.client.get("/healthz")
        self.assertIn("no-cache", resp["Cache-Control"])

    @patch("core.health._check_redis")
    def test_readyz_ok_when_dependencies_up(self, _mock_redis):
        resp = self.client.get("/readyz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {"status": "ok", "checks": {"database": "ok", "redis": "ok"}},
        )

    @patch("core.health._check_redis", side_effect=ConnectionError("redis://secret-host"))
    def test_readyz_503_when_redis_down_and_no_details_leaked(self, _mock_redis):
        with self.assertLogs("core.health", level="ERROR"):
            resp = self.client.get("/readyz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["checks"]["redis"], "error")
        self.assertNotIn("secret-host", resp.content.decode())

    @patch("core.health._check_redis")
    @patch("core.health._check_database", side_effect=Exception("db down"))
    def test_readyz_503_when_database_down(self, _mock_db, _mock_redis):
        with self.assertLogs("core.health", level="ERROR"):
            resp = self.client.get("/readyz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["checks"]["database"], "error")

    def test_health_endpoints_reject_post(self):
        self.assertEqual(self.client.post("/healthz").status_code, 405)
