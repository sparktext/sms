# syntax=docker/dockerfile:1
#
# SparkText production image - multi-stage build.
#
#   Stage 1 "css"     : Node   -> compiles Tailwind CSS          (Node never reaches the final image)
#   Stage 2 "builder" : Python -> installs dependencies into a venv (pip/build tools stay behind)
#   Stage 3 "runtime" : Python -> app code + venv + compiled CSS, runs as a non-root user
#
# ONE image runs every process: web (default CMD), ai_worker and campaign_worker (CMD overridden
# in docker-compose). Build once, run anywhere: local, CI, staging, production.

# Keep in sync with your local virtualenv and CI (version parity).
ARG PYTHON_VERSION=3.15
ARG NODE_VERSION=24

# -----------------------------------------------------------------------------
# Stage 1: compile Tailwind CSS
# -----------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS css
WORKDIR /build
# Copy ONLY the dependency manifests first: this layer is cached until they change,
# so a code-only change doesn't re-download node packages.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
# Tailwind scans the templates to decide which CSS classes to generate.
COPY . .
RUN npm run build:css

# -----------------------------------------------------------------------------
# Stage 2: install Python dependencies into an isolated virtualenv
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-trixie AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
# Compiler toolchain for packages without a prebuilt wheel for this Python version
# (e.g. autobahn on 3.15). It exists ONLY in this stage: the runtime image receives the
# compiled venv, never gcc - fewer tools for an attacker, ~250 MB smaller image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# Same caching trick: requirements first, code later.
COPY requirements.txt .
RUN pip install -r requirements.txt

# -----------------------------------------------------------------------------
# Stage 3: the runtime image that actually ships
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-trixie AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Unprivileged user with a fixed UID: if the app is ever compromised, the attacker is not root.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app . .
# Freshly compiled CSS overrides whatever is in the repo.
COPY --from=css --chown=app:app /build/static/css ./static/css

# Collect + hash + compress static files at BUILD time, so every container starts instantly
# with identical assets. Settings fail fast without SECRET_KEY/DATABASE_URL, so we pass
# throwaway build-only values; they exist only for this single RUN step (no DB connection is made).
RUN SECRET_KEY=build-only-not-secret \
    DATABASE_URL=postgres://build:build@localhost:5432/build \
    python manage.py collectstatic --noinput \
 && mkdir -p /app/media \
 && chown -R app:app /app/staticfiles /app/media

USER app

EXPOSE 8000

# Liveness probe. Python's stdlib is used instead of curl, so no extra package is installed.
# NOTE: requests come from 127.0.0.1, so production ALLOWED_HOSTS must include 127.0.0.1.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# --proxy-headers: trust X-Forwarded-For from the reverse proxy so Django sees the REAL client IP.
# Without it, every request appears to come from the proxy and django-axes would lock out
# ALL users after 5 failed logins from anyone.
CMD ["daphne", "--proxy-headers", "-b", "0.0.0.0", "-p", "8000", "sparktext.asgi:application"]
