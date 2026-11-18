"""PROMPT 073 Security Correction: Sidecar Replay Defense Persistence & Concurrency Test Suite.

Verifies:
1. Replay detection survives process restarts using PersistentReplayStore.
2. Concurrent duplicate requests are safely rejected without race conditions.
3. Expired messages are rejected based on timestamp drift.
4. New authenticated sessions with unique session tokens.
5. Storage failures fail closed (SidecarSecurityError, never fail-open).
6. Valid messages after reconnection succeed.
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.communication.errors import SidecarSecurityError
from src.communication.sidecar import (
    PersistentReplayStore,
    SecureSidecarCodec,
    SecureSidecarHost,
    SidecarEnvelope,
    SidecarEventType,
)


def test_replay_after_process_restart(tmp_path: Path):
    """Verify that previously accepted nonces are rejected after the host process restarts."""
    db_file = tmp_path / "sidecar_replay.db"
    secret = "k" * 32

    # Instance 1: Host process runs and accepts registration + message
    host1 = SecureSidecarHost(auth_secret=secret, storage_path=db_file)
    codec = SecureSidecarCodec(auth_secret=secret)

    reg_envelope = SidecarEnvelope(
        type=SidecarEventType.REGISTER,
        correlation_id="corr-reg-1",
        nonce="nonce-reg-1",
        payload={"sidecar_id": "test_sidecar_node"},
    )
    host1.process_incoming_line(codec.encode(reg_envelope))

    msg1 = SidecarEnvelope(
        type=SidecarEventType.INBOUND_MESSAGE,
        correlation_id="corr-msg-1",
        nonce="nonce-msg-1",
        payload={"text": "Message before restart"},
    )
    encoded_msg1 = codec.encode(msg1)
    host1.process_incoming_line(encoded_msg1)

    # Simulate process restart: create Host 2 using the same persistent storage
    host2 = SecureSidecarHost(auth_secret=secret, storage_path=db_file)
    assert host2.is_registered is False
    assert host2.session_id != host1.session_id

    # Re-register host2 with fresh correlation/nonce
    reg2_envelope = SidecarEnvelope(
        type=SidecarEventType.REGISTER,
        correlation_id="corr-reg-2",
        nonce="nonce-reg-2",
        payload={"sidecar_id": "test_sidecar_node"},
    )
    host2.process_incoming_line(codec.encode(reg2_envelope))

    # Attacker replays message 1 (from previous session) -> MUST be rejected because nonce was persisted
    with pytest.raises(SidecarSecurityError, match="Replay detected: nonce"):
        host2.process_incoming_line(encoded_msg1)


def test_duplicate_simultaneous_requests(tmp_path: Path):
    """Verify that concurrent threads attempting to process the same nonce simultaneously cannot race."""
    db_file = tmp_path / "concurrent_replay.db"
    secret = "k" * 32
    host = SecureSidecarHost(auth_secret=secret, storage_path=db_file)
    codec = SecureSidecarCodec(auth_secret=secret)

    reg_envelope = SidecarEnvelope(
        type=SidecarEventType.REGISTER,
        correlation_id="c-reg",
        nonce="n-reg",
        payload={"sidecar_id": "concurrent_node"},
    )
    host.process_incoming_line(codec.encode(reg_envelope))

    msg = SidecarEnvelope(
        type=SidecarEventType.INBOUND_MESSAGE,
        correlation_id="corr-race-1",
        nonce="nonce-race-1",
        payload={"text": "Racing duplicate"},
    )
    encoded_msg = codec.encode(msg)

    # Launch 10 simultaneous threads trying to submit identical message
    results = []
    errors = []

    def submit():
        try:
            res = host.process_incoming_line(encoded_msg)
            results.append(res)
        except SidecarSecurityError as e:
            errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(submit) for _ in range(10)]
        concurrent.futures.wait(futures)

    # Exactly 1 must succeed, and 9 must fail with Replay detected
    assert len(results) == 1
    assert len(errors) == 9
    for err in errors:
        assert "Replay detected" in str(err)


def test_expired_messages_rejected():
    """Verify that envelopes with expired timestamps are rejected before storage."""
    secret = "k" * 32
    codec = SecureSidecarCodec(auth_secret=secret, max_drift_seconds=60.0)

    past_time = (datetime.now(timezone.utc) - timedelta(seconds=180)).isoformat()
    old_envelope = SidecarEnvelope(
        type=SidecarEventType.HEARTBEAT,
        correlation_id="c-exp",
        nonce="n-exp",
        timestamp=past_time,
        payload={"status": "OK"},
    )
    encoded = codec.encode(old_envelope)

    with pytest.raises(SidecarSecurityError, match="outside allowed time window"):
        codec.decode(encoded)


def test_new_authenticated_sessions():
    """Verify that new host instances create cryptographically distinct session tokens."""
    secret = "k" * 32
    host_a = SecureSidecarHost(auth_secret=secret)
    host_b = SecureSidecarHost(auth_secret=secret)

    assert host_a.session_id != host_b.session_id
    assert len(host_a.session_id) == 32

    codec = SecureSidecarCodec(auth_secret=secret)
    reg_env = SidecarEnvelope(
        type=SidecarEventType.REGISTER,
        correlation_id="c-session-1",
        nonce="n-session-1",
        payload={"sidecar_id": "node_session"},
    )
    host_a.process_incoming_line(codec.encode(reg_env))
    ack_line = host_a.create_registration_ack(reg_env.correlation_id)

    decoded_ack = codec.decode(ack_line)
    assert decoded_ack.payload["session_id"] == host_a.session_id
    assert decoded_ack.payload["session_generation"] == 1


def test_storage_failure_fails_closed():
    """Verify that any database or storage error triggers SidecarSecurityError (never fail-open)."""
    secret = "k" * 32
    mock_store = MagicMock(spec=PersistentReplayStore)
    mock_store.check_and_record.side_effect = sqlite3.OperationalError("Disk I/O error or corruption")

    host = SecureSidecarHost(auth_secret=secret, replay_store=mock_store)
    codec = SecureSidecarCodec(auth_secret=secret)

    msg = SidecarEnvelope(
        type=SidecarEventType.REGISTER,
        correlation_id="c-fail",
        nonce="n-fail",
        payload={"sidecar_id": "test_node"},
    )
    encoded = codec.encode(msg)

    # Must fail closed with SidecarSecurityError
    with pytest.raises(SidecarSecurityError):
        host.process_incoming_line(encoded)


def test_valid_messages_after_reconnection(tmp_path: Path):
    """Verify that after disconnect and reconnect, legitimate new messages succeed."""
    db_file = tmp_path / "reconnect_replay.db"
    secret = "k" * 32
    codec = SecureSidecarCodec(auth_secret=secret)

    # Session 1
    host1 = SecureSidecarHost(auth_secret=secret, storage_path=db_file)
    host1.process_incoming_line(
        codec.encode(
            SidecarEnvelope(
                type=SidecarEventType.REGISTER,
                correlation_id="c-s1-reg",
                nonce="n-s1-reg",
                payload={"sidecar_id": "persistent_node"},
            )
        )
    )
    host1.process_incoming_line(
        codec.encode(
            SidecarEnvelope(
                type=SidecarEventType.INBOUND_MESSAGE,
                correlation_id="c-s1-m1",
                nonce="n-s1-m1",
                payload={"text": "First session msg"},
            )
        )
    )

    # Session 2 (reconnection)
    host2 = SecureSidecarHost(auth_secret=secret, storage_path=db_file)
    host2.process_incoming_line(
        codec.encode(
            SidecarEnvelope(
                type=SidecarEventType.REGISTER,
                correlation_id="c-s2-reg",
                nonce="n-s2-reg",
                payload={"sidecar_id": "persistent_node"},
            )
        )
    )
    res = host2.process_incoming_line(
        codec.encode(
            SidecarEnvelope(
                type=SidecarEventType.INBOUND_MESSAGE,
                correlation_id="c-s2-m1",
                nonce="n-s2-m1",
                payload={"text": "Second session msg"},
            )
        )
    )
    assert res.type == SidecarEventType.INBOUND_MESSAGE
    assert res.payload["text"] == "Second session msg"
