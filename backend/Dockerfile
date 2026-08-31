# Backend production image.
#
# Multi-stage: the builder resolves dependencies from the uv lockfile into a
# self-contained virtualenv; the runtime stage copies only that venv plus the
# alembic setup. The app package is installed into the venv (non-editable), so
# no source tree ships in the final image.
#
# Deliberately NOT installed: openssh-client, git, docker, certbot, curl. The
# backend invokes no local binaries (all SSH and key work is in-process via
# asyncssh) and writes nothing to the local filesystem. ca-certificates is
# required for TLS to Postgres (Supabase), Clerk, GitHub, and Sentry.
#
# No .env or secrets are ever copied in (see .dockerignore); configuration
# reaches the container at runtime via the compose env_file.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependency layer first, so source changes do not bust the resolver cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Install the app package itself, non-editable, so /app/.venv is self-contained.
COPY app ./app
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim

# TLS trust store for asyncpg (ssl.create_default_context) and httpx.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 10001 --create-home appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
# Migrations run from this same image as a discrete step
# (alembic upgrade head); alembic/env.py imports app.config from the venv.
COPY alembic.ini ./
COPY alembic ./alembic

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER appuser

EXPOSE 8000

# Stdlib-only liveness probe; curl is intentionally absent from this image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
