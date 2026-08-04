"""Logging behaviour tests: request correlation and secret redaction.

No DB and no network. A loguru sink captures records in memory so we can assert
both that the request middleware stamps a correlation id onto every log line of a
request, and that secret-handling code never lets a secret reach the log.
"""

import contextlib
import logging
import shlex

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.logging_config import logger, setup_logging
from app.utils.redaction import redact_secrets
from app.middleware.logging import RequestLoggingMiddleware
from app.services.github_service import GithubService
from app.services.server_reregistration_service import generate_bootstrap_password


@contextlib.contextmanager
def capture_logs():
    """Yield a list that collects every loguru record emitted inside the block."""
    records: list[dict] = []
    sink_id = logger.add(lambda message: records.append(message.record), level="DEBUG")
    try:
        yield records
    finally:
        logger.remove(sink_id)


def _mini_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/thing")
    async def thing():
        logger.info("inside route handler")
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    app.add_middleware(RequestLoggingMiddleware)
    return app


@pytest.mark.asyncio
async def test_request_id_is_bound_and_propagates():
    """One request id is stamped on the in/out lines AND the route's own log."""
    app = _mini_app()
    with capture_logs() as records:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/thing")
    assert resp.status_code == 200

    messages = [r["message"] for r in records]
    assert "request start GET /api/thing" in messages
    assert "inside route handler" in messages
    assert any(m.startswith("request done GET /api/thing") for m in messages)

    # Every line of this request shares one real (non-placeholder) request id.
    request_ids = {
        r["extra"]["request_id"]
        for r in records
        if r["message"]
        in ("request start GET /api/thing", "inside route handler")
        or r["message"].startswith("request done")
    }
    assert len(request_ids) == 1
    assert "-" not in request_ids


@pytest.mark.asyncio
async def test_health_requests_are_not_logged():
    """The liveness probe must not flood the log with in/out lines."""
    app = _mini_app()
    with capture_logs() as records:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            await ac.get("/api/health")
    assert not any(r["message"].startswith("request ") for r in records)


class _FakeAsyncClient:
    """Replays a single canned response; mirrors test_github_service's fake."""

    def __init__(self, response: httpx.Response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, headers=None, json=None):
        return self._response

    async def delete(self, url, headers=None):
        return self._response


@pytest.mark.asyncio
async def test_github_token_is_never_logged(mocker):
    """add_deploy_key logs the repo and key id, never the OAuth token."""
    sentinel_token = "gho_SENTINEL_SECRET_TOKEN_DO_NOT_LOG"
    response = httpx.Response(
        201,
        json={"id": 4242},
        request=httpx.Request("POST", "https://api.github.com/x"),
    )
    mocker.patch(
        "app.services.github_service.httpx.AsyncClient",
        side_effect=lambda **kwargs: _FakeAsyncClient(response),
    )

    with capture_logs() as records:
        key_id = await GithubService().add_deploy_key(
            sentinel_token, "octocat/hello", "Abstract: hello", "ssh-ed25519 AAAA"
        )

    assert key_id == 4242
    # The success line names the repo and key id (message is already interpolated).
    assert any(
        r["message"] == "GitHub deploy key added: repo=octocat/hello key_id=4242"
        for r in records
    )
    # The token never appears in any message or interpolated argument.
    for record in records:
        assert sentinel_token not in record["message"]
        for arg in record.get("extra", {}).values():
            assert sentinel_token != arg


def test_redact_masks_chpasswd_root_password():
    """A generated root password embedded in the chpasswd command asyncssh logs is
    masked, while the command stays recognizable (still shows root: and chpasswd)."""
    password = generate_bootstrap_password()
    # Built exactly as server_deletion_service does before handing it to asyncssh.
    reset_script = f"printf %s {shlex.quote(f'root:{password}')} | chpasswd"
    command = f"sudo sh -c {shlex.quote(reset_script)}"

    redacted = redact_secrets(f"  Command: {command}")

    assert password not in redacted
    assert "chpasswd" in redacted
    assert "root:***" in redacted


def test_redact_leaves_ordinary_commands_and_user_group_pairs_intact():
    """Redaction must not mangle normal command logging (the reason we keep it)."""
    for command in (
        "cd /srv/app && docker compose up -d --build",
        "chown -R root:root /etc/nginx",
        "grep -qF 'github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQ' ~/.ssh/known_hosts",
    ):
        assert redact_secrets(f"  Command: {command}") == f"  Command: {command}"


def test_redact_masks_github_token():
    assert "gho_" not in redact_secrets("token=gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def test_asyncssh_command_is_redacted_through_the_intercept_handler(monkeypatch):
    """End to end: a secret in a stdlib asyncssh log record is scrubbed on its way
    into loguru, not just when _redact is called directly."""
    # setup_logging is idempotent; reset the guard so it rewires the intercept handler.
    monkeypatch.setattr("app.logging_config._configured", False)
    setup_logging()

    password = generate_bootstrap_password()
    with capture_logs() as records:
        logging.getLogger("asyncssh").info(
            "  Command: %s", f"printf %s 'root:{password}' | chpasswd"
        )

    messages = [r["message"] for r in records]
    assert any("root:***" in m for m in messages)
    assert all(password not in m for m in messages)
