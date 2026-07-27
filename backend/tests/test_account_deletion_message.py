"""Unit tests for the clean, user-facing message a blocking server produces during
account deletion. No DB or SSH: exercises _blocking_server_message directly to prove
internal step names never leak."""

from app.services.account_deletion_service import _blocking_server_message


def test_unreachable_server_message_names_server_and_the_fix():
    msg = _blocking_server_message("web1", "connect_ssh")
    assert "web1" in msg
    assert "couldn't be reached" in msg
    assert "powered on and online" in msg


def test_other_step_failure_uses_generic_teardown_message():
    msg = _blocking_server_message("web1", "revoke_sudoers")
    assert "web1" in msg
    assert "couldn't be fully torn down" in msg


def test_message_never_leaks_internal_step_name():
    for step in ("connect_ssh", "restore_ssh_access", "revoke_sudoers", "delete_project"):
        msg = _blocking_server_message("web1", step)
        assert step not in msg
        assert "step" not in msg.lower()
