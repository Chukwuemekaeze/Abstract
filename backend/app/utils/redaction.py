"""Shared secret redaction.

A single best-effort scrubber for secret shapes that could appear verbatim in text
we did not author — library log lines (asyncssh logs every command it runs at INFO,
so a command embedding a credential inline would otherwise leak) and Sentry event
payloads. Both the logging sink and the monitoring `before_send` hook call
`redact_secrets` so there is one source of truth for what a secret looks like.

These are a safety net, not a guarantee: they only catch the shapes listed here. Our
own code avoids logging secrets in the first place.
"""

import re

# GitHub tokens (ghp_/gho_/ghs_/ghr_/ghu_ ...).
_GITHUB_TOKEN = re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b")
_BEARER = re.compile(r"(?i)(authorization:\s*bearer\s+)\S+")
# The password half of a `user:password` credential fed to chpasswd. Passwords never
# contain whitespace, quotes, or a pipe, so the match stops cleanly at the shell
# delimiter; the length floor avoids masking unix `user:group` pairs (chown root:root).
_CHPASSWD_CRED = re.compile(r"(\b[\w.-]+:)([^\s'\"|]{8,})")


def redact_secrets(message: str) -> str:
    """Mask known secret shapes in a message, preserving surrounding context so the
    line still shows what ran. Best-effort: only the patterns above are covered."""
    if "chpasswd" in message:
        message = _CHPASSWD_CRED.sub(r"\1***", message)
    message = _GITHUB_TOKEN.sub("***", message)
    message = _BEARER.sub(r"\1***", message)
    return message
