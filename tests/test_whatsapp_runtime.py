"""Tests for WhatsApp Agent Runtime Wiring, Preflight Checks, Lifecycle, and Simulation.

Canonical Reference:
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md
    AEGIS Prompt 036 (Phase 9C.3, Phase 2)
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from src.gateway.drafts import DraftManager
from src.gateway.whatsapp_runtime import (
    get_whatsapp_status,
    mask_jid,
    run_live_whatsapp,
    run_preflight,
    run_simulated_whatsapp,
)
from src.models.domain import ModelDescriptor, ProviderType
from src.security.contact_key_store import ContactKeyStore
from src.security.dpapi import WindowsDPAPIKeyProtector
from src.storage.database import initialize_runtime_database
from src.storage.migrations import (
    bootstrap_schema_migrations,
    apply_migrations,
    load_migrations_from_directory,
)


@pytest.fixture
def runtime_migrations_dir() -> Path:
    return Path(__file__).parent.parent / "migrations"


@pytest.fixture
def populated_db_conn(tmp_path: Path, runtime_migrations_dir: Path):
    db_file = tmp_path / "test_sage.db"
    conn = initialize_runtime_database(db_file, migrations_dir=runtime_migrations_dir)
    with conn:
        conn.execute(
            """
            INSERT INTO whatsapp_contact_settings (contact_jid, label, style_mode, updated_at)
            VALUES ('15550100001@s.whatsapp.net', 'Alice', 'banter', '2026-11-26T00:00:00Z')
            """
        )
    yield conn, db_file
    try:
        conn.close()
    except Exception:
        pass


@pytest.fixture
def fake_bridge_dir(tmp_path: Path) -> Path:
    b_dir = tmp_path / "bridge"
    b_dir.mkdir(parents=True, exist_ok=True)
    (b_dir / "node_modules").mkdir(parents=True, exist_ok=True)
    (b_dir / "package.json").write_text(
        json.dumps({"name": "bridge", "engines": {"node": ">=22"}}), encoding="utf-8"
    )
    (b_dir / "index.js").write_text("// stub bridge", encoding="utf-8")
    return b_dir


# ==============================================================================
# 1. PREFLIGHT CHECKS (2.2 & Prompt 9 T1)
# ==============================================================================


def test_preflight_all_checks_pass(
    populated_db_conn, fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    conn, db_file = populated_db_conn
    auth_dir = tmp_path / "auth"
    owner_jid = "15550100099@s.whatsapp.net"

    def fake_llm(p, s):
        return "ok"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.10.0\n")
        passed, err = run_preflight(
            db_path=db_file,
            migrations_dir=runtime_migrations_dir,
            bridge_dir=fake_bridge_dir,
            auth_dir=auth_dir,
            owner_jid=owner_jid,
            custom_llm=fake_llm,
            db_conn=conn,
        )

    assert passed is True
    assert err is None


def test_preflight_check1_sqlcipher_inactive_fails(
    fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    """PRAGMA cipher_version empty or None fails closed."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = (None,)
    mock_conn.execute.return_value = mock_cur

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.0.0\n")
        passed, err = run_preflight(
            db_path=tmp_path / "fake.db",
            migrations_dir=runtime_migrations_dir,
            bridge_dir=fake_bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid="15550100099@s.whatsapp.net",
            custom_llm=lambda p, s: "ok",
            db_conn=mock_conn,
        )
    assert passed is False
    assert "SQLCipher is not active" in err


def test_preflight_check1_plaintext_db_fails(
    fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    """Plaintext SQLite database file fails closed."""
    db_file = tmp_path / "plaintext.db"
    plain_conn = sqlite3.connect(str(db_file))
    plain_conn.execute("CREATE TABLE t (id INT);")
    plain_conn.commit()
    plain_conn.close()

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = ("4.19.0 community",)
    mock_conn.execute.return_value = mock_cur

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.0.0\n")
        passed, err = run_preflight(
            db_path=db_file,
            migrations_dir=runtime_migrations_dir,
            bridge_dir=fake_bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid="15550100099@s.whatsapp.net",
            custom_llm=lambda p, s: "ok",
            db_conn=mock_conn,
        )
    assert passed is False
    assert "plaintext SQLite" in err


def test_preflight_check2_owner_in_contact_whitelist_fails(
    populated_db_conn, fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    """Owner JID in contact whitelist fails closed."""
    conn, db_file = populated_db_conn
    owner_jid = "15550100099@s.whatsapp.net"
    # Insert owner JID into contact settings
    with conn:
        conn.execute(
            """
            INSERT INTO whatsapp_contact_settings (contact_jid, label, style_mode, updated_at)
            VALUES (?, 'OwnerContact', 'normal', '2026-11-26T00:00:00Z')
            """,
            (owner_jid,),
        )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.0.0\n")
        passed, err = run_preflight(
            db_path=db_file,
            migrations_dir=runtime_migrations_dir,
            bridge_dir=fake_bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid=owner_jid,
            custom_llm=lambda p, s: "ok",
            db_conn=conn,
        )
    assert passed is False
    assert "Owner JID" in err
    assert "present in the contact whitelist" in err


def test_preflight_check1_db_missing(
    tmp_path, fake_bridge_dir, runtime_migrations_dir
):
    missing_db = tmp_path / "nonexistent.db"
    passed, err = run_preflight(
        db_path=missing_db,
        migrations_dir=runtime_migrations_dir,
        bridge_dir=fake_bridge_dir,
        auth_dir=tmp_path / "auth",
        owner_jid="15550100099@s.whatsapp.net",
        custom_llm=lambda p, s: "ok",
    )
    assert passed is False
    assert "Preflight Check 1 Failed" in err
    assert "does not exist" in err


def test_preflight_check1_schema_outdated(
    tmp_path, fake_bridge_dir, runtime_migrations_dir
):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = ("4.19.0 community",)
    mock_conn.execute.return_value = mock_cur

    with patch("src.gateway.whatsapp_runtime.get_current_schema_version", return_value=0):
        passed, err = run_preflight(
            db_path=tmp_path / "outdated.db",
            migrations_dir=runtime_migrations_dir,
            bridge_dir=fake_bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid="15550100099@s.whatsapp.net",
            custom_llm=lambda p, s: "ok",
            db_conn=mock_conn,
        )
    assert passed is False
    assert "Preflight Check 1 Failed" in err
    assert "schema is at version 0" in err


def test_preflight_check2_no_whitelisted_contacts(
    tmp_path, fake_bridge_dir, runtime_migrations_dir
):
    db_file = tmp_path / "empty_contacts.db"
    conn = initialize_runtime_database(db_file, migrations_dir=runtime_migrations_dir)
    # No contacts inserted
    passed, err = run_preflight(
        db_path=db_file,
        migrations_dir=runtime_migrations_dir,
        bridge_dir=fake_bridge_dir,
        auth_dir=tmp_path / "auth",
        owner_jid="15550100099@s.whatsapp.net",
        custom_llm=lambda p, s: "ok",
        db_conn=conn,
    )
    conn.close()
    assert passed is False
    assert "Preflight Check 2 Failed" in err
    assert "No contacts whitelisted" in err


def test_preflight_check3_owner_jid_invalid_or_missing(
    populated_db_conn, fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    conn, db_file = populated_db_conn

    # Missing
    passed, err = run_preflight(
        db_path=db_file,
        migrations_dir=runtime_migrations_dir,
        bridge_dir=fake_bridge_dir,
        auth_dir=tmp_path / "auth",
        owner_jid=None,
        custom_llm=lambda p, s: "ok",
        db_conn=conn,
    )
    assert passed is False
    assert "Preflight Check 3 Failed" in err
    assert "Owner JID is not configured" in err

    # Invalid (@lid or invalid characters)
    passed2, err2 = run_preflight(
        db_path=db_file,
        migrations_dir=runtime_migrations_dir,
        bridge_dir=fake_bridge_dir,
        auth_dir=tmp_path / "auth",
        owner_jid="123456@lid",
        custom_llm=lambda p, s: "ok",
        db_conn=conn,
    )
    assert passed2 is False
    assert "Preflight Check 3 Failed" in err2
    assert "invalid" in err2.lower()


def test_preflight_check4_node_missing_or_too_old(
    populated_db_conn, fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    conn, db_file = populated_db_conn
    owner_jid = "15550100099@s.whatsapp.net"

    # Node not found on PATH
    with patch("subprocess.run", side_effect=FileNotFoundError):
        passed, err = run_preflight(
            db_path=db_file,
            migrations_dir=runtime_migrations_dir,
            bridge_dir=fake_bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid=owner_jid,
            custom_llm=lambda p, s: "ok",
            db_conn=conn,
        )
    assert passed is False
    assert "Preflight Check 4 Failed" in err
    assert "not found on PATH" in err

    # Node version < 22 (e.g. v20.10.0 against >=22 in package.json)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v20.10.0\n")
        passed2, err2 = run_preflight(
            db_path=db_file,
            migrations_dir=runtime_migrations_dir,
            bridge_dir=fake_bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid=owner_jid,
            custom_llm=lambda p, s: "ok",
            db_conn=conn,
        )
    assert passed2 is False
    assert "Preflight Check 4 Failed" in err2
    assert "Node.js >= 22 is required" in err2


def test_preflight_check5_node_modules_missing(
    populated_db_conn, tmp_path, runtime_migrations_dir
):
    conn, db_file = populated_db_conn
    empty_bridge = tmp_path / "bridge_empty"
    empty_bridge.mkdir()
    (empty_bridge / "package.json").write_text(
        json.dumps({"name": "bridge", "engines": {"node": ">=22"}}), encoding="utf-8"
    )
    # node_modules not created

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.0.0\n")
        passed, err = run_preflight(
            db_path=db_file,
            migrations_dir=runtime_migrations_dir,
            bridge_dir=empty_bridge,
            auth_dir=tmp_path / "auth",
            owner_jid="15550100099@s.whatsapp.net",
            custom_llm=lambda p, s: "ok",
            db_conn=conn,
        )
    assert passed is False
    assert "Preflight Check 5 Failed" in err
    assert "node_modules" in err


def test_preflight_check6_refuses_cloud_model(
    populated_db_conn, fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    conn, db_file = populated_db_conn
    owner_jid = "15550100099@s.whatsapp.net"

    # Gateway configured with CLOUD model provider
    mock_gateway = MagicMock()
    mock_registry = MagicMock()
    mock_descriptor = ModelDescriptor(
        model_id="gpt-4o",
        provider_id="openai-cloud",
        display_name="GPT-4o Cloud",
        provider_type=ProviderType.CLOUD,
    )
    mock_registry.get_model.return_value = mock_descriptor
    mock_registry.list_models.return_value = []
    mock_gateway.registry = mock_registry

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.0.0\n")
        passed, err = run_preflight(
            db_path=db_file,
            migrations_dir=runtime_migrations_dir,
            bridge_dir=fake_bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid=owner_jid,
            model_gateway=mock_gateway,
            model_alias="gpt-4o",
            custom_llm=None,
            db_conn=conn,
        )

    assert passed is False
    assert "Preflight Check 6 Failed" in err
    assert "CLOUD" in err
    assert "strictly refuses to run unless model provider is LOCAL" in err


def test_preflight_check6_live_model_unreachable_fails(
    populated_db_conn, fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    """In live mode, if model health ping reports unhealthy or unreachable, preflight fails closed."""
    from src.models.domain import ProviderHealth, ProviderHealthStatus

    conn, db_file = populated_db_conn
    owner_jid = "15550100099@s.whatsapp.net"

    mock_gateway = MagicMock()
    mock_registry = MagicMock()
    mock_descriptor = ModelDescriptor(
        model_id="qwen35-4b-q4km",
        provider_id="local-offline",
        display_name="Qwen 3.5 4B Local",
        provider_type=ProviderType.LOCAL,
    )
    mock_registry.get_model.return_value = mock_descriptor
    mock_gateway.registry = mock_registry
    mock_gateway.get_provider_health.return_value = ProviderHealth(
        provider_id="local-offline",
        status=ProviderHealthStatus.UNAVAILABLE,
        error_message="daemon not responding on port 11434",
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.0.0\n")
        passed, err = run_preflight(
            db_path=db_file,
            migrations_dir=runtime_migrations_dir,
            bridge_dir=fake_bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid=owner_jid,
            model_gateway=mock_gateway,
            model_alias="qwen35-4b-q4km",
            custom_llm=None,
            db_conn=conn,
            simulate_mode=False,
        )

    assert passed is False
    assert "Preflight Check 6 Failed" in err
    assert "unreachable" in err


def test_preflight_check6_live_model_timeout_fails(
    populated_db_conn, fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    """In live mode, if model health ping exceeds 5s timeout, preflight fails closed."""
    import time

    conn, db_file = populated_db_conn
    owner_jid = "15550100099@s.whatsapp.net"

    mock_gateway = MagicMock()
    mock_registry = MagicMock()
    mock_descriptor = ModelDescriptor(
        model_id="qwen35-4b-q4km",
        provider_id="local-offline",
        display_name="Qwen 3.5 4B Local",
        provider_type=ProviderType.LOCAL,
    )
    mock_registry.get_model.return_value = mock_descriptor
    mock_gateway.registry = mock_registry

    def slow_ping(prov):
        time.sleep(6.0)
        return None

    mock_gateway.get_provider_health.side_effect = slow_ping

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.0.0\n")
        passed, err = run_preflight(
            db_path=db_file,
            migrations_dir=runtime_migrations_dir,
            bridge_dir=fake_bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid=owner_jid,
            model_gateway=mock_gateway,
            model_alias="qwen35-4b-q4km",
            custom_llm=None,
            db_conn=conn,
            simulate_mode=False,
        )

    assert passed is False
    assert "Preflight Check 6 Failed" in err
    assert "timed out after 5.0 seconds" in err


def test_preflight_check7_auth_dir_not_writable(
    populated_db_conn, fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    conn, db_file = populated_db_conn
    owner_jid = "15550100099@s.whatsapp.net"
    auth_dir = tmp_path / "readonly_auth"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.0.0\n")
        with patch.object(Path, "write_text", side_effect=PermissionError("Access denied")):
            passed, err = run_preflight(
                db_path=db_file,
                migrations_dir=runtime_migrations_dir,
                bridge_dir=fake_bridge_dir,
                auth_dir=auth_dir,
                owner_jid=owner_jid,
                custom_llm=lambda p, s: "ok",
                db_conn=conn,
            )

    assert passed is False
    assert "Preflight Check 7 Failed" in err
    assert "not writable" in err


# ==============================================================================
# 2. STATUS SUBCOMMAND (2.4)
# ==============================================================================


def test_whatsapp_status_reporting(populated_db_conn, tmp_path):
    conn, _ = populated_db_conn
    with conn:
        conn.execute(
            """
            INSERT INTO whatsapp_gateway_state (key, value, updated_at)
            VALUES ('bridge_status', 'CONNECTED', '2026-11-26T12:00:00Z'),
                   ('paired_phone', '+919876543210', '2026-11-26T12:00:00Z'),
                   ('bridge_start_time', '2026-11-26T12:00:00Z', '2026-11-26T12:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO whatsapp_drafts (
                draft_id, contact_jid, inbound_message_id, draft_ciphertext,
                draft_nonce, text_sha256, status, created_at, expires_at,
                decided_at, sent_message_id, edit_count
            ) VALUES
            ('D1', '15550100001@s.whatsapp.net', 'm1', x'00', x'00', 'h1', 'pending', '2026-11-26T12:00:00Z', '2026-11-26T18:00:00Z', NULL, NULL, 0),
            ('D2', '15550100001@s.whatsapp.net', 'm2', x'00', x'00', 'h2', 'approved', '2026-11-26T12:00:00Z', '2026-11-26T18:00:00Z', '2026-11-26T12:05:00Z', 's1', 0),
            ('D3', '15550100001@s.whatsapp.net', 'm3', x'00', x'00', 'h3', 'rejected', '2026-11-26T12:00:00Z', '2026-11-26T18:00:00Z', '2026-11-26T12:10:00Z', NULL, 0)
            """
        )

    status = get_whatsapp_status(conn)
    assert status["bridge_state"] == "CONNECTED"
    assert status["connected_phone"] == "+919876543210"
    assert status["whitelisted_contacts"] == 1
    assert status["pending_drafts"] == 1
    assert status["today_approved"] == 1
    assert status["today_rejected"] == 1


# ==============================================================================
# 3. SCRIPTED DEMO SCENARIO (--simulate) (2.5)
# ==============================================================================


def test_full_simulated_scenario_run():
    """Verify 5-turn demo scenario plays through with 0 unsanctioned sends."""
    res = run_simulated_whatsapp()
    assert res == 0


# ==============================================================================
# 4. LIFECYCLE, BACKOFF & CLEAN SHUTDOWN (2.3)
# ==============================================================================


def test_clean_shutdown_on_stop_event(
    populated_db_conn, fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    conn, db_file = populated_db_conn
    stop_event = threading.Event()
    # Trigger stop immediately
    stop_event.set()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.0.0\n")
        code = run_live_whatsapp(
            db_path=db_file,
            auth_dir=tmp_path / "auth",
            bridge_dir=fake_bridge_dir,
            migrations_dir=runtime_migrations_dir,
            owner_jid="15550100099@s.whatsapp.net",
            custom_llm=lambda p, s: "ok",
            stop_event=stop_event,
        )

    assert code == 0


def test_bridge_crash_backoff_and_exit(
    populated_db_conn, fake_bridge_dir, runtime_migrations_dir, tmp_path
):
    conn, db_file = populated_db_conn
    stop_event = threading.Event()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="v22.0.0\n")
        with patch("src.gateway.whatsapp_runtime.BaileysSidecarSupervisor.start", side_effect=Exception("Bridge crashed")):
            code = run_live_whatsapp(
                db_path=db_file,
                auth_dir=tmp_path / "auth",
                bridge_dir=fake_bridge_dir,
                migrations_dir=runtime_migrations_dir,
                owner_jid="15550100099@s.whatsapp.net",
                custom_llm=lambda p, s: "ok",
                stop_event=stop_event,
                max_consecutive_failures=2,
            )

    # Reached maximum consecutive failures and exited
    assert code == 1


def test_mask_jid():
    assert mask_jid("15550100001@s.whatsapp.net") == "...0001@s.whatsapp.net"
    assert mask_jid("12@s.whatsapp.net") == "***@s.whatsapp.net"
    assert mask_jid("") == "<unknown>"
