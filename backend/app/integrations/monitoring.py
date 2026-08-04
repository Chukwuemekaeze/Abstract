"""Application monitoring: error tracking, tracing, and profiling.

The single boundary to Sentry. This is the only module in the app that imports
`sentry_sdk`; everything else calls the provider-agnostic helpers below. Swapping
Sentry for another provider means rewriting this file alone — no call site changes.

`init_monitoring` is called once, at the topmost level in `main.py`, before the app
starts serving. FastAPI/Starlette request spans and SQLAlchemy query spans are
auto-instrumented by the SDK once initialized; the helpers here cover the explicit
surface (capturing errors, attributing events to a user, ad-hoc custom spans).

Note: Sentry retired its standalone custom-metrics product; performance "metrics" are
derived from the traces/spans captured here.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import sentry_sdk

from app.config import Settings
from app.utils.redaction import redact_secrets


def init_monitoring(settings: Settings) -> None:
    """Initialize Sentry from settings. Idempotent-safe to call once at startup.

    With no DSN configured the SDK initializes inert (events are dropped), so dev and
    test environments need no special casing.
    """
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        release=settings.sentry_release,
        send_default_pii=settings.sentry_send_default_pii,
        # Fraction of requests turned into performance transactions.
        traces_sample_rate=settings.sentry_traces_sample_rate,
        # Continuous profiling: sample sessions, and let the profiler run automatically
        # whenever there is an active transaction ("trace" lifecycle).
        profile_session_sample_rate=settings.sentry_profiles_sample_rate,
        profile_lifecycle="trace",
        # Scrub known secret shapes from every outgoing event, mirroring the log sink.
        before_send=_before_send,
    )


def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Mask secrets in the message and exception values of an event before it leaves.

    Reuses the same `redact_secrets` scrubber as the logging sink so a token or inline
    credential that reaches Sentry is masked the same way it would be in the logs.
    """
    logentry = event.get("logentry")
    if isinstance(logentry, dict) and isinstance(logentry.get("message"), str):
        logentry["message"] = redact_secrets(logentry["message"])

    for exception in event.get("exception", {}).get("values", []):
        if isinstance(exception.get("value"), str):
            exception["value"] = redact_secrets(exception["value"])

    return event


def capture_exception(exc: Exception, **context: Any) -> None:
    """Report an exception, tagging it with any provided key/value context."""
    if context:
        sentry_sdk.set_context("extra", context)
    sentry_sdk.capture_exception(exc)


def capture_message(message: str, level: str = "info", **context: Any) -> None:
    """Report a standalone message at the given severity level."""
    if context:
        sentry_sdk.set_context("extra", context)
    sentry_sdk.capture_message(message, level=level)


def set_user(user_id: str) -> None:
    """Attribute subsequent events in this scope to a user, mirroring log correlation."""
    sentry_sdk.set_user({"id": user_id})


def clear_user() -> None:
    """Detach the current user from the scope."""
    sentry_sdk.set_user(None)


@contextmanager
def span(op: str, name: str) -> Iterator[None]:
    """Wrap a hot path (SSH run, deploy) in a custom trace span for performance detail."""
    with sentry_sdk.start_span(op=op, name=name):
        yield
