"""External log shipping wiring tests: gating and sink configuration.

No network and no real BetterStack SDK calls: LogtailHandler and logger.add are patched
so we assert only the wiring (does a sink get attached, and with what options) rather
than the vendor transport.
"""

import logging
from types import SimpleNamespace

from app.integrations import external_logging


def _settings(token=None, host=None, level="INFO"):
    """Minimal stand-in exposing only the fields init_external_logging reads."""
    return SimpleNamespace(
        external_logging_token=token,
        external_logging_host=host,
        external_logging_level=level,
    )


def test_inert_without_token(mocker):
    """No token => no SDK touched and no sink attached, even if a host is present."""
    add = mocker.patch.object(external_logging.logger, "add")
    handler = mocker.patch.object(external_logging, "LogtailHandler")

    external_logging.init_external_logging(_settings(token=None, host="https://ingest"))

    handler.assert_not_called()
    add.assert_not_called()


def test_inert_without_host(mocker):
    """A token alone is not enough: BetterStack ingests per source, so host is required."""
    add = mocker.patch.object(external_logging.logger, "add")
    handler = mocker.patch.object(external_logging, "LogtailHandler")

    external_logging.init_external_logging(_settings(token="src-token", host=None))

    handler.assert_not_called()
    add.assert_not_called()


def test_attaches_remote_sink_when_configured(mocker):
    """Token + host => the handler is built from them and added as a non-blocking,
    structured sink at the configured remote level."""
    add = mocker.patch.object(external_logging.logger, "add")
    handler_cls = mocker.patch.object(external_logging, "LogtailHandler")

    external_logging.init_external_logging(
        _settings(token="src-token", host="https://ingest.example", level="WARNING")
    )

    handler_cls.assert_called_once_with(
        source_token="src-token", host="https://ingest.example"
    )
    add.assert_called_once()
    sink_arg = add.call_args.args[0]
    kwargs = add.call_args.kwargs
    # The sink is a callable wrapping the handler, not the handler itself.
    assert callable(sink_arg)
    assert kwargs["level"] == "WARNING"
    assert kwargs["enqueue"] is True


def test_sink_maps_loguru_record_to_flat_logrecord():
    """The sink flattens a loguru record onto a stdlib LogRecord: clean message, real
    origin, and request_id/user_id as top-level attributes for the aggregator to index."""
    captured: list[logging.LogRecord] = []

    class _CapturingHandler:
        def handle(self, record: logging.LogRecord) -> None:
            captured.append(record)

    sink = external_logging._make_sink(_CapturingHandler())

    loguru_record = {
        "name": "app.routes.servers",
        "level": SimpleNamespace(no=logging.WARNING),
        "file": SimpleNamespace(path="/app/routes/servers.py"),
        "line": 42,
        "function": "list_servers",
        "message": "request start GET /api/projects",
        "exception": None,
        "extra": {"request_id": "0d045a426c22", "user_id": "user_1"},
    }
    sink(SimpleNamespace(record=loguru_record))

    assert len(captured) == 1
    rec = captured[0]
    assert rec.getMessage() == "request start GET /api/projects"
    assert rec.levelno == logging.WARNING
    assert rec.pathname == "/app/routes/servers.py"
    assert rec.lineno == 42
    assert rec.funcName == "list_servers"
    assert rec.request_id == "0d045a426c22"
    assert rec.user_id == "user_1"
