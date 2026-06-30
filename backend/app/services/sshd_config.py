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


def _edit_script(directive: str, value: str, value_alternatives: str) -> str:
    """Build the idempotent edit that makes `directive value` the effective setting.

    Two parts:
      1. Normalize the directive in the main /etc/ssh/sshd_config (replace any
         existing, possibly commented, line then ensure it is present). -i.bak keeps a
         recovery copy. This is what governs on systems without config drop-ins.
      2. On systems whose main config Includes /etc/ssh/sshd_config.d/*.conf (cloud
         images like DigitalOcean and Ubuntu ship a 50-cloud-init.conf that sets
         PasswordAuthentication yes), the Include is read before the main body and
         sshd uses the FIRST value it sees, so our main-file edit is ignored. Write a
         drop-in that sorts first (00-abstract-...) so our value wins.
    """
    target = f"{directive} {value}"
    dropin = f"/etc/ssh/sshd_config.d/00-abstract-{directive}.conf"
    return (
        f"sed -ri.bak 's/^[#[:space:]]*{directive}[[:space:]]+"
        f"({value_alternatives})[[:space:]]*.*$/{target}/' /etc/ssh/sshd_config && "
        f"(grep -qE '^{directive} {value}' /etc/ssh/sshd_config || "
        f"echo '{target}' >> /etc/ssh/sshd_config) && "
        f"if grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\\.d/' "
        f"/etc/ssh/sshd_config; then mkdir -p /etc/ssh/sshd_config.d && "
        f"printf '%s\\n' '{target}' > {dropin}; fi"
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
    await run(_edit_script(directive, value, value_alternatives))
    await run(RELOAD_SSHD)
    result = await run(_verify_script(directive))
    output = (result.stdout or "").strip().lower()
    if output == "sshd_t_unavailable":
        return SshdDirectiveResult.UNAVAILABLE
    if f"{directive.lower()} {value}" in output:
        return SshdDirectiveResult.OK
    return SshdDirectiveResult.MISMATCH
