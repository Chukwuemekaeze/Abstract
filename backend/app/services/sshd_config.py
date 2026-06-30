"""Shared sshd_config directive helper.

Setting a single sshd_config directive idempotently and confirming the running daemon
picked it up is needed in three places: install_key (PasswordAuthentication, during
onboarding) and the hardening operations disable_root_login (PermitRootLogin) and
disable_password_auth (PasswordAuthentication). The command text and the verification
parsing live here as the single source of truth.

Each caller supplies a `run` coroutine that decides how a command is executed (raw, or
wrapped with a sudo prefix and `sh -c`) and which exception to raise when a command
exits nonzero. This function returns the semantic verification outcome so the caller
can map it to its own warning or error. This module deliberately depends on nothing
else in the app, so both ssh_service and hardening_service can import it.
"""

from __future__ import annotations

import enum
from typing import Any, Awaitable, Callable

# Reload sshd, trying the common service manager names in order. The final branch
# exits nonzero with a clear marker so a runner that checks exit status raises.
RELOAD_SSHD = (
    "systemctl reload sshd 2>/dev/null || "
    "systemctl reload ssh 2>/dev/null || "
    "service ssh reload 2>/dev/null || "
    "service sshd reload 2>/dev/null || "
    "(echo no_reload_method_succeeded >&2; exit 1)"
)


class SshdDirectiveResult(enum.Enum):
    OK = "ok"  # runtime config confirms the directive
    UNAVAILABLE = "unavailable"  # sshd -T not available, file edit unconfirmed
    MISMATCH = "mismatch"  # runtime config disagrees with the intended value


def _sed_script(directive: str, value: str, value_alternatives: str) -> str:
    # Replace any existing (possibly commented) directive line whose value is one of
    # value_alternatives. -i.bak keeps a recovery copy at sshd_config.bak.
    target = f"{directive} {value}"
    return (
        f"sed -ri.bak 's/^[#[:space:]]*{directive}[[:space:]]+"
        f"({value_alternatives})[[:space:]]*.*$/{target}/' /etc/ssh/sshd_config"
    )


def _ensure_script(directive: str, value: str) -> str:
    target = f"{directive} {value}"
    return (
        f"grep -qE '^{directive} {value}' /etc/ssh/sshd_config || "
        f"echo '{target}' >> /etc/ssh/sshd_config"
    )


def _verify_script(directive: str) -> str:
    return (
        f"sshd -T 2>/dev/null | grep -i '^{directive.lower()}' || "
        "echo sshd_t_unavailable"
    )


async def apply_sshd_directive(
    run: Callable[[str], Awaitable[Any]],
    *,
    directive: str,
    value: str,
    value_alternatives: str,
) -> SshdDirectiveResult:
    """Set an sshd_config directive and verify the running daemon reports it.

    Steps: replace the directive line, ensure it is present, reload sshd, then read
    `sshd -T`. `run` executes one shell script and returns its completed result (with
    a `.stdout` attribute); it must raise if the command exits nonzero. The file edit
    and reload can both succeed while the daemon still reports the old value, so the
    runtime check via `sshd -T` is what we trust.
    """
    await run(_sed_script(directive, value, value_alternatives))
    await run(_ensure_script(directive, value))
    await run(RELOAD_SSHD)
    result = await run(_verify_script(directive))
    output = (result.stdout or "").strip().lower()
    if output == "sshd_t_unavailable":
        return SshdDirectiveResult.UNAVAILABLE
    if f"{directive.lower()} {value}" in output:
        return SshdDirectiveResult.OK
    return SshdDirectiveResult.MISMATCH
