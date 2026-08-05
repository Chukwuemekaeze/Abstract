"""External log shipping: forward the application log stream to a remote aggregator.

The single boundary to BetterStack. This is the only module in the app that imports
`logtail`; nothing else knows which provider ingests the logs. Swapping BetterStack for
another aggregator means rewriting this file alone — the config surface stays
provider-agnostic (`external_logging_*`) and no call site changes.

Unlike Sentry, this is not a separate product with its own lifecycle: it is simply
another destination for the same loguru stream. `init_external_logging` attaches a
second sink to the global `logger`; `logging_config.py` keeps owning the local console
sink, the request/user context patcher, and redaction-on-intercept. Because every sink
sees the record only after the intercept handler has already scrubbed library lines
(e.g. the commands asyncssh logs), the remote sink inherits that redaction for free.

The sink maps each loguru record onto a plain stdlib LogRecord rather than shipping
loguru's serialized envelope. `LogtailHandler` (include_extra_attributes on by default)
then surfaces the fields flat: the message stays human-readable, request_id/user_id
become top-level queryable attributes, and file/line point at the real call site
instead of loguru's internals.

`init_external_logging` is called once at startup, in `main.py`, immediately after
`setup_logging` has installed the console sink. With no token configured the SDK is
never touched and the app logs only to stderr, so dev and test need no special casing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from logtail import LogtailHandler
from loguru import logger

from app.config import Settings

if TYPE_CHECKING:
    from loguru import Message


def _make_sink(handler: logging.Handler) -> Callable[[Message], None]:
    """Build a loguru sink that forwards records to a stdlib handler as flat LogRecords.

    Copies the loguru record's real origin (name/file/line/level) and stamps the
    request_id/user_id correlation context as top-level attributes, so the aggregator
    indexes them as fields rather than burying them inside a serialized blob.
    """

    def sink(message: Message) -> None:
        record = message.record
        std = logging.LogRecord(
            name=record["name"] or __name__,
            level=record["level"].no,
            pathname=record["file"].path,
            lineno=record["line"],
            msg=record["message"],
            args=(),
            exc_info=record["exception"],
            func=record["function"],
        )
        std.request_id = record["extra"].get("request_id", "-")
        std.user_id = record["extra"].get("user_id", "-")
        handler.handle(std)

    return sink


def init_external_logging(settings: Settings) -> None:
    """Attach the remote log sink if configured; a no-op otherwise.

    Must run after `setup_logging` (which calls `logger.remove()`), or the sink it adds
    here would be wiped. Requires both a source token and an ingesting host: BetterStack
    ingests per source, so there is no universal host to fall back to.
    """
    if not settings.external_logging_token or not settings.external_logging_host:
        return

    handler = LogtailHandler(
        source_token=settings.external_logging_token,
        host=settings.external_logging_host,
    )
    logger.add(
        _make_sink(handler),
        level=settings.external_logging_level,
        # Ship on a background queue: a slow or unreachable aggregator must never block
        # request handling.
        enqueue=True,
    )
