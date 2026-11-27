"""Unit tests for WhatsApp Baileys session folder hardening and unlink subcommand.

Validates:
- Pure icacls command construction
- Pure icacls verification output parser with valid and invalid ACL principal sets
- Windows session folder hardening and non-Windows graceful skip
- Session unlinking safeguards (runtime lockfile refusal, confirmation prompt, force --yes)
- Post-unlink owner reminder message
"""

import os
import platform
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.cli import handle_whatsapp_cli
from src.gateway.whatsapp_runtime import (
    build_icacls_harden_command,
    build_icacls_verify_command,
    harden_session_directory,
    is_pid_alive,
    parse_and_verify_icacls_output,
    unlink_whatsapp_session,
)


def test_build_icacls_harden_command():
    """Verify build_icacls_harden_command constructs expected icacls arguments."""
    p = Path("C:/data/session")
    cmd = build_icacls_harden_command(p, user_domain="TESTDOM", username="alice")
    assert cmd == [
        "icacls",
        str(p),
        "/inheritance:r",
        "/grant:r",
        "TESTDOM\\alice:(OI)(CI)F",
    ]

    # Without domain
    cmd2 = build_icacls_harden_command(p, user_domain="", username="alice")
    assert cmd2 == [
        "icacls",
        str(p),
        "/inheritance:r",
        "/grant:r",
        "alice:(OI)(CI)F",
    ]


def test_build_icacls_verify_command():
    """Verify build_icacls_verify_command returns ['icacls', path]."""
    p = Path("C:/data/session")
    assert build_icacls_verify_command(p) == ["icacls", str(p)]


def test_parse_and_verify_icacls_output_clean():
    """Output with only allowed user passes verification."""
    sample = """
C:\\data\\session DOMAIN\\alice:(OI)(CI)(F)

Successfully processed 1 files; Failed processing 0 files
"""
    valid, unauth = parse_and_verify_icacls_output(sample, allowed_user="DOMAIN\\alice")
    assert valid is True
    assert unauth == []


def test_parse_and_verify_icacls_output_system_and_admins():
    """Output with allowed user, SYSTEM, and Administrators passes verification."""
    sample = """
C:\\data\\session DOMAIN\\alice:(OI)(CI)(F)
                 NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)
                 BUILTIN\\Administrators:(I)(OI)(CI)(F)

Successfully processed 1 files; Failed processing 0 files
"""
    valid, unauth = parse_and_verify_icacls_output(sample, allowed_user="DOMAIN\\alice")
    assert valid is True
    assert unauth == []


def test_parse_and_verify_icacls_output_unauthorized_principals():
    """Output containing unauthorized principals fails verification."""
    sample = """
C:\\data\\session DOMAIN\\alice:(OI)(CI)(F)
                 Everyone:(OI)(CI)(R)
                 DOMAIN\\OtherUsers:(OI)(CI)(M)
                 NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)

Successfully processed 1 files; Failed processing 0 files
"""
    valid, unauth = parse_and_verify_icacls_output(sample, allowed_user="DOMAIN\\alice")
    assert valid is False
    assert "Everyone" in unauth
    assert "DOMAIN\\OtherUsers" in unauth
    assert "NT AUTHORITY\\SYSTEM" not in unauth


def test_parse_and_verify_icacls_output_empty():
    """Empty icacls output fails verification."""
    valid, unauth = parse_and_verify_icacls_output("", allowed_user="DOMAIN\\alice")
    assert valid is False
    assert "<empty_output>" in unauth


def test_harden_session_directory_skips_on_non_windows(tmp_path):
    """On non-Windows platforms, session hardening logs and returns success without error."""
    with patch("platform.system", return_value="Linux"):
        ok, err = harden_session_directory(tmp_path / "session")
        assert ok is True
        assert err is None


def test_harden_session_directory_windows_mocked_success(tmp_path):
    """On Windows, successful icacls execution and clean verification returns (True, None)."""
    sess = tmp_path / "session"
    sess.mkdir()

    clean_output = f"{sess} DOMAIN\\user:(OI)(CI)(F)\nSuccessfully processed 1 files"

    with patch("platform.system", return_value="Windows"):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="Successfully processed 1 files", stderr=""),
                MagicMock(returncode=0, stdout=clean_output, stderr=""),
            ]
            with patch.dict(os.environ, {"USERDOMAIN": "DOMAIN", "USERNAME": "user"}):
                ok, err = harden_session_directory(sess)
                assert ok is True
                assert err is None


def test_harden_session_directory_windows_mocked_unauthorized_fails(tmp_path):
    """On Windows, if verification finds unauthorized principals, hardening fails closed."""
    sess = tmp_path / "session"
    sess.mkdir()

    bad_output = f"{sess} DOMAIN\\user:(OI)(CI)(F)\n  Everyone:(OI)(CI)(R)"

    with patch("platform.system", return_value="Windows"):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="Successfully processed 1 files", stderr=""),
                MagicMock(returncode=0, stdout=bad_output, stderr=""),
            ]
            with patch.dict(os.environ, {"USERDOMAIN": "DOMAIN", "USERNAME": "user"}):
                ok, err = harden_session_directory(sess)
                assert ok is False
                assert "unauthorized access permissions" in err
                assert "Everyone" in err


def test_unlink_refuses_when_runtime_active(tmp_path, capsys):
    """unlink_whatsapp_session refuses deletion when runtime lockfile has an active PID."""
    auth_dir = tmp_path / "channels" / "whatsapp" / "session"
    auth_dir.mkdir(parents=True)
    (auth_dir / "creds.json").write_text("{}", encoding="utf-8")

    lock_file = auth_dir.parent / "runtime.lock"
    # Use current process PID which is guaranteed alive
    lock_file.write_text(str(os.getpid()), encoding="utf-8")

    ret = unlink_whatsapp_session(auth_dir=auth_dir, force_yes=True)
    assert ret == 1
    captured = capsys.readouterr()
    assert "Cannot unlink WhatsApp session while runtime is running" in captured.err
    assert auth_dir.exists()


def test_unlink_nonexistent_directory(tmp_path, capsys):
    """unlink_whatsapp_session succeeds gracefully when session dir does not exist."""
    auth_dir = tmp_path / "nonexistent_session"
    ret = unlink_whatsapp_session(auth_dir=auth_dir, force_yes=True)
    assert ret == 0
    captured = capsys.readouterr()
    assert "No WhatsApp session found" in captured.out
    assert "Settings > Linked devices" in captured.out


def test_unlink_interactive_cancelled(tmp_path, capsys):
    """unlink_whatsapp_session cancels deletion when user does not confirm 'yes'."""
    auth_dir = tmp_path / "session"
    auth_dir.mkdir()
    (auth_dir / "creds.json").write_text("{}", encoding="utf-8")

    ret = unlink_whatsapp_session(
        auth_dir=auth_dir,
        force_yes=False,
        confirm_input_fn=lambda: "no",
    )
    assert ret == 1
    captured = capsys.readouterr()
    assert "Unlink cancelled" in captured.out
    assert auth_dir.exists()


def test_unlink_interactive_confirmed(tmp_path, capsys):
    """unlink_whatsapp_session deletes directory and prints reminder when confirmed."""
    auth_dir = tmp_path / "session"
    auth_dir.mkdir()
    (auth_dir / "creds.json").write_text("{}", encoding="utf-8")

    ret = unlink_whatsapp_session(
        auth_dir=auth_dir,
        force_yes=False,
        confirm_input_fn=lambda: "yes",
    )
    assert ret == 0
    captured = capsys.readouterr()
    assert "Successfully deleted WhatsApp session directory" in captured.out
    assert "Settings > Linked devices" in captured.out
    assert not auth_dir.exists()


def test_unlink_cli_integration(tmp_path, capsys):
    """Verify `python -m src.cli whatsapp unlink --yes --auth-dir <path>` deletes session."""
    auth_dir = tmp_path / "cli_session"
    auth_dir.mkdir()
    (auth_dir / "creds.json").write_text("{}", encoding="utf-8")

    ret = handle_whatsapp_cli(["unlink", "--yes", "--auth-dir", str(auth_dir)])
    assert ret == 0
    captured = capsys.readouterr()
    assert "Successfully deleted WhatsApp session directory" in captured.out
    assert "Settings > Linked devices" in captured.out
    assert not auth_dir.exists()
