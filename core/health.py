"""
Health endpoints used by Docker, the reverse proxy and the CD pipeline.

/healthz  - LIVENESS:  "Is the process alive and able to serve a request?"
            No external dependencies. If this fails, restarting the container can help.

/readyz   - READINESS: "Can this instance do real work right now?"
            Checks the database and Redis. If this fails, restarting the container will NOT
            help (the dependency is down), so it must not be used as a restart trigger. The CD
            pipeline uses it after a deploy to decide: keep the new version, or roll back.

Neither endpoint requires login, and failure details go to the logs, never to the response,
so the endpoints don't leak hostnames or error messages to the internet.
"""
import logging

import redis
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


def _check_database():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


def _check_redis():
    client = redis.Redis.from_url(
        settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2
    )
    try:
        client.ping()
    finally:
        client.close()


@never_cache
@require_GET
def healthz(request):
    return JsonResponse({"status": "ok"})


@never_cache
@require_GET
def readyz(request):
    checks = {}
    for name, check in (("database", _check_database), ("redis", _check_redis)):
        try:
            check()
            checks[name] = "ok"
        except Exception:
            logger.exception("Readiness check failed: %s", name)
            checks[name] = "error"

    healthy = all(result == "ok" for result in checks.values())
    return JsonResponse(
        {"status": "ok" if healthy else "error", "checks": checks},
        status=200 if healthy else 503,
    )
