"""Central logging utility.

All application logging goes through the loguru `logger` exported here. Import it
anywhere with `from app.logging_config import logger`. setup_logging configures the
sink once and routes the standard library logging used by uvicorn, asyncssh, and
SQLAlchemy into loguru so every line shares one format.

Every record is enriched with the current request's `request_id` and `user_id` (from
context vars set by the request middleware and the auth dependency) so a single log
line, or a whole multi-step SSH flow, can be traced back to the HTTP request and user
that caused it. When no request is in scope (startup/shutdown) both fall back to "-".
"""

import logging
import re
import sys
from contextvars import ContextVar

from loguru import logger

# Standard library loggers whose records should be funneled into loguru.
_INTERCEPTED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "asyncssh",
    "sqlalchemy.engine",
)

# Per-request correlation context. Set by the logging middleware (request_id) and the
# auth dependency (user_id); read by the patcher below on every emitted record.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


def bind_request_id(request_id: str) -> None:
    """Bind the current request's id so every downstream log line carries it."""
    request_id_var.set(request_id)


def bind_user_id(user_id: str) -> None:
    """Bind the resolved user's id for the remainder of the request."""
    user_id_var.set(user_id)


def _inject_context(record: dict) -> None:
    """loguru patcher: stamp each record with the current request/user context.

    Falls back to "-" so records emitted outside any request (startup, shutdown,
    background) format cleanly instead of raising KeyError on the extra fields.
    """
    record["extra"]["request_id"] = request_id_var.get() or "-"
    record["extra"]["user_id"] = user_id_var.get() or "-"


_PRETTY_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
    "<level>{level: <8}</level> "
    "<cyan>req={extra[request_id]}</cyan> <cyan>user={extra[user_id]}</cyan> "
    "<level>{message}</level>"
)


# Backstop patterns for secrets that could appear verbatim in a log line we do not
# author. asyncssh logs every command it runs at INFO, so a command that embeds a
# credential inline (e.g. the chpasswd root-password reset) would otherwise land in
# the log unredacted — our own code avoids logging secrets, but library logging does
# not. These are a safety net, not a guarantee: they only catch shapes listed here.
_GITHUB_TOKEN = re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b")
_BEARER = re.compile(r"(?i)(authorization:\s*bearer\s+)\S+")
# The password half of a `user:password` credential fed to chpasswd. Passwords never
# contain whitespace, quotes, or a pipe, so the match stops cleanly at the shell
# delimiter; the length floor avoids masking unix `user:group` pairs (chown root:root).
_CHPASSWD_CRED = re.compile(r"(\b[\w.-]+:)([^\s'\"|]{8,})")


def _redact(message: str) -> str:
    """Mask known secret shapes in a log message, preserving surrounding context so
    the line still shows what ran. Best-effort: only the patterns above are covered."""
    if "chpasswd" in message:
        message = _CHPASSWD_CRED.sub(r"\1***", message)
    message = _GITHUB_TOKEN.sub("***", message)
    message = _BEARER.sub(r"\1***", message)
    return message


class _InterceptHandler(logging.Handler):
    """Forwards standard library log records to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Walk back to the frame that issued the log so loguru reports the real caller.
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, _redact(record.getMessage())
        )


_configured = False


def setup_logging(level: str = "INFO", fmt: str = "pretty") -> None:
    """Configure loguru and intercept standard library logging. Idempotent.

    level: minimum level emitted (e.g. "INFO", "DEBUG").
    fmt: "json" for structured one-object-per-line output (prod), anything else for
    the human-readable colored format (dev).
    """
    global _configured
    if _configured:
        return

    logger.remove()
    # The patcher stamps request/user context on every record; the default extra keeps
    # {extra[...]} references safe for records emitted before any context is bound.
    logger.configure(patcher=_inject_context, extra={"request_id": "-", "user_id": "-"})

    if fmt == "json":
        logger.add(sys.stderr, level=level, serialize=True, backtrace=False, diagnose=False)
    else:
        logger.add(
            sys.stderr,
            level=level,
            format=_PRETTY_FORMAT,
            backtrace=False,
            diagnose=False,
        )

    handler = _InterceptHandler()
    logging.basicConfig(handlers=[handler], level=0, force=True)
    for name in _INTERCEPTED_LOGGERS:
        std_logger = logging.getLogger(name)
        std_logger.handlers = [handler]
        std_logger.propagate = False

    _configured = True


__all__ = [
    "logger",
    "setup_logging",
    "bind_request_id",
    "bind_user_id",
    "request_id_var",
    "user_id_var",
]
