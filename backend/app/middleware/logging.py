"""Request logging middleware.

A pure ASGI middleware (deliberately not Starlette's BaseHTTPMiddleware, which runs the
endpoint in a separate task and would break context-var propagation) that assigns each
request a short correlation id, binds it so every downstream log line carries it, and
logs the request in and out with its status and duration.

The path is logged but never the query string, which can carry tokens. user_id is bound
later, inside the auth dependency, once the request's user row is resolved.
"""

import time
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_config import bind_request_id, logger

# Liveness probes would otherwise flood the log; skip their in/out lines entirely.
_SKIP_PATHS = frozenset({"/api/health"})


class RequestLoggingMiddleware:
    """Assign a request id, bind it into the log context, and log in/out + timing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex[:12]
        bind_request_id(request_id)

        method = scope.get("method", "-")
        path = scope.get("path", "-")
        skip = path in _SKIP_PATHS

        if not skip:
            logger.info("request start {} {}", method, path)

        started = time.monotonic()
        status_code = 500

        async def _send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            if not skip:
                duration_ms = (time.monotonic() - started) * 1000
                logger.info(
                    "request done {} {} status={} duration_ms={:.1f}",
                    method,
                    path,
                    status_code,
                    duration_ms,
                )
