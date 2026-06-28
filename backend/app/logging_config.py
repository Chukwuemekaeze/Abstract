"""Central logging utility.

All application logging goes through the loguru `logger` exported here. Import it
anywhere with `from app.logging_config import logger`. setup_logging configures the
sink once and routes the standard library logging used by uvicorn, asyncssh, and
SQLAlchemy into loguru so every line shares one format.
"""

import logging
import sys

from loguru import logger

# Standard library loggers whose records should be funneled into loguru.
_INTERCEPTED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "asyncssh",
    "sqlalchemy.engine",
)


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
            level, record.getMessage()
        )


_configured = False


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru and intercept standard library logging. Idempotent."""
    global _configured
    if _configured:
        return

    logger.remove()
    logger.add(sys.stderr, level=level, backtrace=False, diagnose=False)

    handler = _InterceptHandler()
    logging.basicConfig(handlers=[handler], level=0, force=True)
    for name in _INTERCEPTED_LOGGERS:
        std_logger = logging.getLogger(name)
        std_logger.handlers = [handler]
        std_logger.propagate = False

    _configured = True


__all__ = ["logger", "setup_logging"]
