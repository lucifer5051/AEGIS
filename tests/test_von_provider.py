"""AEGIS Fast Decision Engine - Von Provider Unit Test Suite.

Prompt: 014A
Scope: Validates Von provider pinning, storage boundary safety, manifest checksum verification,
offline execution by construction, non-blocking admission concurrency, residency reuse,
and latency percentile metrics.
"""

import hashlib
import json
import socket
import threading
from pathlib import Path
from typing import Any, Dict

import pytest

from src.decision.errors import (
    ModelIntegrityError,
)
from src.decision.models import (
    DecisionOutcome,
    DecisionRoute,
    FallbackReason,
    FastDecisionRequest,
    ProviderStatus,
)
from src.decision.providers.von import (
    VON_AUDITED_COMMIT,
    VonFastDecisionProvider,
)
from src.storage.paths import get_default_aegis_home


def test_01_pinned_source_commit_constant():
    """Assert Von audited commit is pinned to exact frozen commit."""
    assert VON_AUDITED_COMMIT == "bed7e7337791c5124a557eb59ad918cb6747b276"
    provider = VonFastDecisionProvider()
    assert provider.audited_commit == "bed7e7337791c5124a557eb59ad918cb6747b276"


def test_02_default_checkpoint_path_resolution():
    """Assert default checkpoint directory is inside
    %USERPROFILE%\\.aegis\\models\\fast-decision."""
    provider = VonFastDecisionProvider()
    expected = (get_default_aegis_home() / "models" / "fast-decision" / "von" / "1.1.0").resolve()
    assert provider.checkpoint_dir == expected


def test_03_storage_boundary_onedrive_rejection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Assert checkpoint directory inside OneDrive raises ModelIntegrityError."""
    fake_od_path = tmp_path / "OneDrive" / ".aegis" / "models" / "von"
    fake_od_path.mkdir(parents=True)
    provider = VonFastDecisionProvider(checkpoint_dir=fake_od_path)

    with pytest.raises(ModelIntegrityError, match="OneDrive"):
        provider.validate_storage_boundaries()


def test_04_storage_boundary_repo_rejection():
    """Assert checkpoint directory inside Git repository raises ModelIntegrityError."""
    repo_root = Path(__file__).resolve().parent.parent
    inside_repo = repo_root / "models" / "von"
    provider = VonFastDecisionProvider(checkpoint_dir=inside_repo)

    with pytest.raises(ModelIntegrityError, match="Git repository"):
        provider.validate_storage_boundaries()


def test_05_storage_boundary_recovery_rejection():
    """Assert checkpoint directory inside C:\\AEGIS raises ModelIntegrityError."""
    inside_recovery = Path("C:/AEGIS/models/von").resolve()
    provider = VonFastDecisionProvider(checkpoint_dir=inside_recovery)

    with pytest.raises(ModelIntegrityError, match="C:\\\\AEGIS"):
        provider.validate_storage_boundaries()


def test_06_missing_checkpoint_fails_closed(tmp_path: Path):
    """Assert non-existent checkpoint directory causes verify_integrity to return False."""
    missing_dir = tmp_path / "non_existent_model_dir"
    provider = VonFastDecisionProvider(checkpoint_dir=missing_dir)

    assert provider.verify_integrity() is False
    assert provider.initialize() is False
    assert provider.status == ProviderStatus.DISABLED


def test_07_manifest_checksum_verification_success_and_tamper_detection(tmp_path: Path):
    """Assert manifest SHA-256 verification succeeds on exact match and fails on tampering."""
    model_dir = tmp_path / "models" / "von" / "1.1.0"
    model_dir.mkdir(parents=True)

    # Create dummy files
    content_a = b"dummy_config_content"
    content_b = b"dummy_weights_content"
    sha_a = hashlib.sha256(content_a).hexdigest()
    sha_b = hashlib.sha256(content_b).hexdigest()

    file_a = model_dir / "config.json"
    file_b = model_dir / "weights.data"
    file_a.write_bytes(content_a)
    file_b.write_bytes(content_b)

    # Create trusted manifest
    manifest_data = {
        "manifest_name": "test-von-manifest",
        "manifest_version": "1.0.0",
        "source_commit": VON_AUDITED_COMMIT,
        "required_files": {
            "config.json": {"sha256": sha_a, "required": True},
            "weights.data": {"sha256": sha_b, "required": True},
        },
    }
    manifest_file = tmp_path / "test_manifest.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    provider = VonFastDecisionProvider(
        checkpoint_dir=model_dir,
        manifest_path=manifest_file,
    )

    # Step 1: Verification should succeed
    assert provider.verify_integrity() is True

    # Step 2: Tamper with file_b content
    file_b.write_bytes(b"tampered_content")
    assert provider.verify_integrity() is False

    # Step 3: Remove file_a
    file_a.unlink()
    assert provider.verify_integrity() is False


def test_08_offline_by_construction_no_network_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Assert provider execution makes zero network calls and works completely offline."""

    # Monkeypatch socket to raise an exception if connect is called
    def forbidden_connect(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("NETWORK CALL ATTEMPTED DURING INFERENCE - VIOLATION")

    monkeypatch.setattr(socket.socket, "connect", forbidden_connect)

    # Backend mock that simulates Option-Marker decide
    def mock_backend(user_text: str, choices: Dict[str, str]) -> Dict[str, Any]:
        return {
            "choice": "terminal_intent",
            "confidence": 0.94,
            "scores": {
                "terminal_intent": 0.94,
                "file_intent": 0.04,
                "browser_intent": 0.02,
            },
        }

    provider = VonFastDecisionProvider(
        checkpoint_dir=tmp_path / "valid_dir",
        backend_factory=lambda p: mock_backend,
    )
    # Bypass integrity check for this unit test
    monkeypatch.setattr(provider, "verify_integrity", lambda: True)
    assert provider.initialize() is True

    req = FastDecisionRequest(user_input="git status")
    criteria = {
        DecisionRoute.TERMINAL_INTENT: "Command line",
        DecisionRoute.FILE_INTENT: "Files",
    }
    res = provider.evaluate(req, criteria)

    assert res.outcome == DecisionOutcome.FAST_PATH
    assert res.route == DecisionRoute.TERMINAL_INTENT
    assert res.confidence == 0.94
    assert res.is_advisory_only is True


def test_09_non_blocking_concurrency_admission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Assert concurrent request immediately receives ADMISSION_BUSY fallback without queuing."""
    started_first = threading.Event()
    finish_first = threading.Event()

    def slow_backend(user_text: str, choices: Dict[str, str]) -> Dict[str, Any]:
        started_first.set()
        finish_first.wait(timeout=2.0)
        return {"choice": "file_intent", "confidence": 0.90, "scores": {}}

    provider = VonFastDecisionProvider(
        checkpoint_dir=tmp_path / "valid_dir",
        backend_factory=lambda p: slow_backend,
    )
    monkeypatch.setattr(provider, "verify_integrity", lambda: True)
    assert provider.initialize() is True

    req = FastDecisionRequest(user_input="test request")
    criteria = {DecisionRoute.FILE_INTENT: "Files"}

    # Start first thread
    first_res: Dict[str, Any] = {}

    def run_first() -> None:
        first_res["result"] = provider.evaluate(req, criteria)

    t1 = threading.Thread(target=run_first)
    t1.start()

    # Wait for first thread to enter inference lock
    assert started_first.wait(timeout=1.0) is True

    # Second thread evaluates while first is running: must immediately return ADMISSION_BUSY
    second_res = provider.evaluate(req, criteria)
    assert second_res.outcome == DecisionOutcome.FALLBACK_TO_MANAGER
    assert second_res.fallback_reason == FallbackReason.ADMISSION_BUSY

    # Allow first thread to finish
    finish_first.set()
    t1.join(timeout=2.0)

    assert first_res["result"].outcome == DecisionOutcome.FAST_PATH


def test_10_residency_reuse_across_evaluations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Assert backend is loaded once and resident across multiple evaluations."""
    init_call_count = 0

    def counting_factory(path: Path) -> Any:
        nonlocal init_call_count
        init_call_count += 1
        return lambda text, choices: {"choice": "browser_intent", "confidence": 0.88, "scores": {}}

    provider = VonFastDecisionProvider(
        checkpoint_dir=tmp_path / "valid_dir",
        backend_factory=counting_factory,
    )
    monkeypatch.setattr(provider, "verify_integrity", lambda: True)

    assert provider.initialize() is True
    assert init_call_count == 1

    # Multiple evaluate calls
    req = FastDecisionRequest(user_input="Search web")
    crit = {DecisionRoute.BROWSER_INTENT: "Browser"}
    res1 = provider.evaluate(req, crit)
    res2 = provider.evaluate(req, crit)

    assert res1.outcome == DecisionOutcome.FAST_PATH
    assert res2.outcome == DecisionOutcome.FAST_PATH
    # Factory must not have been called again
    assert init_call_count == 1

    metrics = provider.get_metrics()
    assert metrics.inference_count == 2
    assert metrics.warm_inference_p50_ms > 0.0


def test_11_hardware_qualification_insufficient_ram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Assert hardware qualification fails when available host RAM is below threshold."""
    provider = VonFastDecisionProvider(checkpoint_dir=tmp_path / "valid_dir")
    monkeypatch.setattr(provider, "verify_integrity", lambda: True)

    # Mock hardware profile with 500 MB available RAM (< 1500 MB minimum safe)
    class FakeRAMProfile:
        available_bytes = 500 * 1024 * 1024

    class FakeHardwareProfile:
        ram = FakeRAMProfile()

    qualified = provider.qualify(hardware_profile=FakeHardwareProfile())
    assert qualified is False
    assert provider.status == ProviderStatus.DISABLED
