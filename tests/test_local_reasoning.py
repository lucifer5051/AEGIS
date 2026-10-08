"""Unit & Smoke Tests for SAGE Local Reasoning Model Subsystem.

Canonical Reference:
    schemas/model-manifests/qwen35-4b-q4km.json
    docs/specifications/AEGIS_MODEL_RESOURCE_POLICY.md

Covers:
1. Manifest integrity & configuration schema
2. Strict storage boundary enforcement (anti-OneDrive, anti-repo, anti-C:\\AEGIS)
3. Cryptographic integrity & file verification (exact bytes + SHA-256)
4. Runtime registry & storage deduplication integration
5. Dynamic RAM admission gating & fail-closed behavior
6. Model residency tracking & skill lease protection
7. Minimal offline smoke test (load -> reason -> TTFT/latency/delta -> unload)
8. System prompt conditioning & deterministic generation
9. Hardware qualification profiling & persistence
"""

import json
from pathlib import Path

import pytest

from src.model_policy.concurrency_manager import SkillConcurrencyManager
from src.model_policy.live_benchmark import LiveHardwareBenchmark
from src.model_policy.models import ModelHardwareQualificationProfile
from src.model_policy.qualification import ModelHardwareQualifier
from src.model_policy.residency_manager import ModelResidencyManager
from src.model_policy.runtime_registry import ModelRuntimeRegistry
from src.reasoning import (
    LLAMACPP_CPU_RUNTIME,
    QWEN35_4B_EXACT_BYTES,
    QWEN35_4B_EXPECTED_SHA256,
    QWEN35_4B_FILENAME,
    QWEN35_4B_MODEL,
    SKILL_LOCAL_REASONING,
    LlamaCppReasoningProvider,
    ReasoningAdmissionError,
    ReasoningIntegrityError,
    ReasoningProviderStatus,
    ReasoningRequest,
    ReasoningStorageError,
    build_default_reasoning_provider,
    get_default_local_model_dir,
    qualify_local_reasoning_provider,
    register_reasoning_models,
)


# ==============================================================================
# 1. MANIFEST INTEGRITY & SPECIFICATION
# ==============================================================================
def test_01_manifest_structure_and_values():
    """Verify the source-controlled manifest matches required technical specs."""
    manifest_path = (
        Path(__file__).resolve().parent.parent
        / "schemas"
        / "model-manifests"
        / "qwen35-4b-q4km.json"
    )
    assert manifest_path.is_file(), f"Manifest missing at {manifest_path}"

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["manifest_name"] == "qwen35-4b-q4km"
    assert data["provider"] == "llamacpp"
    assert data["parameter_count"] == "4B"
    assert data["quantization"] == "Q4_K_M"
    assert data["aegis_token_budget_ceiling"] == 4096

    req_files = data["required_files"]
    assert QWEN35_4B_FILENAME in req_files
    file_spec = req_files[QWEN35_4B_FILENAME]
    assert file_spec["size_bytes"] == QWEN35_4B_EXACT_BYTES
    assert file_spec["size_bytes"] == 2_740_937_888
    assert file_spec["sha256"].upper() == QWEN35_4B_EXPECTED_SHA256.upper()
    assert (
        file_spec["sha256"].upper()
        == "00FE7986FF5F6B463E62455821146049DB6F9313603938A70800D1FB69EF11A4"
    )


# ==============================================================================
# 2. STORAGE BOUNDARY ENFORCEMENT
# ==============================================================================
def test_02_storage_boundary_enforcement(tmp_path: Path):
    """Verify forbidden paths (OneDrive, repo root, C:\\AEGIS) are rejected."""
    # Forbidden OneDrive path
    onedrive_dir = tmp_path / "OneDrive" / "models"
    provider_od = LlamaCppReasoningProvider(model_dir=onedrive_dir)
    with pytest.raises(ReasoningStorageError) as exc_od:
        provider_od.verify_storage_boundary()
    assert "OneDrive" in str(exc_od.value)

    # Forbidden Repo path: test both raw and with monkeypatched OneDrive check
    repo_root = Path(__file__).resolve().parent.parent
    provider_repo = LlamaCppReasoningProvider(model_dir=repo_root / "models")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.reasoning.provider.is_path_inside_onedrive", lambda p: False)
        with pytest.raises(ReasoningStorageError) as exc_repo:
            provider_repo.verify_storage_boundary()
        assert "source git repository" in str(exc_repo.value)

    # Valid path outside forbidden roots
    valid_dir = get_default_local_model_dir()
    provider_valid = LlamaCppReasoningProvider(model_dir=valid_dir)
    # Should not raise
    provider_valid.verify_storage_boundary()


# ==============================================================================
# 3. CRYPTOGRAPHIC INTEGRITY VERIFICATION
# ==============================================================================
def test_03_integrity_verification_live_artifact():
    """Verify live copied artifact passes exact byte size and SHA-256 verification."""
    provider = build_default_reasoning_provider()
    assert provider.model_path.is_file(), f"Model file not found at {provider.model_path}"
    assert provider.verify_integrity() is True


def test_04_integrity_verification_tampered_fails(tmp_path: Path):
    """Verify corrupted or truncated artifact fails closed."""
    tampered_dir = tmp_path / "tampered_model"
    tampered_dir.mkdir(parents=True, exist_ok=True)
    fake_file = tampered_dir / QWEN35_4B_FILENAME
    fake_file.write_bytes(b"corrupted_weights")

    provider = LlamaCppReasoningProvider(model_dir=tampered_dir)
    assert provider.verify_integrity() is False

    with pytest.raises(ReasoningIntegrityError):
        provider.initialize()
    assert provider.status == ReasoningProviderStatus.FAILED


# ==============================================================================
# 4. RUNTIME REGISTRY & STORAGE BUDGET
# ==============================================================================
def test_05_runtime_registry_registration():
    """Verify llama.cpp runtime and reasoning model integrate with ModelRuntimeRegistry."""
    registry = ModelRuntimeRegistry()
    register_reasoning_models(registry)

    rt = registry.get_runtime(LLAMACPP_CPU_RUNTIME.runtime_id)
    assert rt is not None
    assert rt.runtime_family.value == "llamacpp"
    assert rt.cuda_capable is False

    model = registry.get_model(QWEN35_4B_MODEL.model_id)
    assert model is not None
    assert model.exact_artifact_bytes == 2_740_937_888

    # Deduplicated runtime footprint
    assert registry.calculate_deduplicated_runtime_footprint() == rt.installed_disk_bytes
    assert registry.calculate_total_artifact_footprint() == 2_740_937_888


# ==============================================================================
# 5. DYNAMIC RAM ADMISSION GATING
# ==============================================================================
def test_06_dynamic_ram_admission_gate_permitted():
    """Verify admission is permitted when system has sufficient RAM headroom."""
    res_mgr = ModelResidencyManager()
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=10 * 1024 * 1024 * 1024,  # 10 GB free
        mock_total_ram_bytes=16 * 1024 * 1024 * 1024,
        mock_free_vram_bytes=4 * 1024 * 1024 * 1024,
        mock_total_vram_bytes=6 * 1024 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
        system_ram_reserve_bytes=1536 * 1024 * 1024,  # 1.5 GB reserve
    )

    eval_result = concurrency_mgr.evaluate_skill_admission(SKILL_LOCAL_REASONING)
    assert eval_result.permitted is True


def test_07_dynamic_ram_admission_gate_denied_fails_closed():
    """Verify model initialization fails closed when RAM headroom is below safe limit."""
    res_mgr = ModelResidencyManager()
    # Mock RAM below system reserve (1.0 GB free < 1.5 GB reserve)
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=1024 * 1024 * 1024,  # 1 GB free
        mock_total_ram_bytes=16 * 1024 * 1024 * 1024,
        mock_free_vram_bytes=4 * 1024 * 1024 * 1024,
        mock_total_vram_bytes=6 * 1024 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
        system_ram_reserve_bytes=1536 * 1024 * 1024,
    )

    eval_result = concurrency_mgr.evaluate_skill_admission(SKILL_LOCAL_REASONING)
    assert eval_result.permitted is False
    assert "RAM memory gate" in eval_result.reason

    provider = build_default_reasoning_provider(
        concurrency_manager=concurrency_mgr,
        residency_manager=res_mgr,
    )
    with pytest.raises(ReasoningAdmissionError) as exc_info:
        provider.initialize()
    assert "resource admission denied" in str(exc_info.value).lower()
    assert provider.status == ReasoningProviderStatus.FAILED


# ==============================================================================
# 6. RESIDENCY TRACKING & LEASE EVECT PROTECTION
# ==============================================================================
def test_08_residency_tracking_and_lease_protection():
    """Verify residency manager prevents unloading models with active skill leases."""
    res_mgr = ModelResidencyManager()
    model_id = "qwen35-4b-q4km"

    # Initially not resident
    assert res_mgr.is_resident(model_id) is False

    # Simulate load
    res_mgr.set_resident(model_id, True)
    assert res_mgr.is_resident(model_id) is True
    assert res_mgr.can_unload(model_id) is True

    # Attach skill lease
    res_mgr.attach_skill_lease(model_id, "lease-test-001")
    assert res_mgr.has_active_leases(model_id) is True
    assert res_mgr.can_unload(model_id) is False

    # Unload attempt while lease active must raise RuntimeError
    with pytest.raises(RuntimeError) as exc_info:
        res_mgr.set_resident(model_id, False)
    assert "Safety violation" in str(exc_info.value)

    # Detach lease -> now unload is permitted
    res_mgr.detach_skill_lease(model_id, "lease-test-001")
    assert res_mgr.can_unload(model_id) is True
    assert res_mgr.unload_model(model_id) is True
    assert res_mgr.is_resident(model_id) is False


# ==============================================================================
# 7. MINIMAL OFFLINE SMOKE TEST
# ==============================================================================
def test_09_offline_smoke_test_reasoning_cycle():
    """Execute complete smoke cycle: load -> reason -> latency/token metrics -> unload."""
    res_mgr = ModelResidencyManager()
    provider = build_default_reasoning_provider(
        residency_manager=res_mgr,
        n_ctx=512,  # Compact context for rapid smoke verification
    )

    try:
        # 1. Initialize & verify ready
        assert provider.initialize() is True
        assert provider.is_ready() is True
        assert res_mgr.is_resident("qwen35-4b-q4km") is True

        # 2. Execute tiny deterministic reasoning prompt
        request = ReasoningRequest(
            prompt="What is 2 + 2? Answer with a single digit.",
            max_tokens=8,
            temperature=0.0,
        )
        response = provider.reason(request)

        # 3. Verify non-empty deterministic output
        assert len(response.content.strip()) > 0
        assert "4" in response.content

        # 4. Verify timing & token metrics
        assert response.first_token_duration_ms > 0.0
        assert response.inference_duration_ms >= response.first_token_duration_ms
        assert response.total_tokens > 0
        assert response.completion_tokens > 0
        assert response.prompt_tokens > 0

        # 5. Check cumulative metrics
        metrics = provider.get_metrics()
        assert metrics.total_inferences == 1
        assert metrics.total_tokens_generated == response.completion_tokens
        assert metrics.last_inference_latency_ms == response.inference_duration_ms

    finally:
        # 6. Unload cleanly
        assert provider.unload() is True
        assert provider.is_ready() is False
        assert provider.status == ReasoningProviderStatus.UNLOADED
        assert res_mgr.is_resident("qwen35-4b-q4km") is False


# ==============================================================================
# 8. SYSTEM PROMPT CONDITIONING
# ==============================================================================
def test_10_system_prompt_reasoning_conditioning():
    """Verify system prompt is accepted and respected in reasoning queries."""
    provider = build_default_reasoning_provider(n_ctx=512)

    try:
        provider.initialize()
        req = ReasoningRequest(
            system_prompt="You are a strict math engine. Output only the numerical answer.",
            prompt="What is 7 * 6?",
            max_tokens=8,
            temperature=0.0,
        )
        resp = provider.reason(req)
        assert len(resp.content.strip()) > 0
        assert "42" in resp.content
    finally:
        provider.unload()


# ==============================================================================
# 9. QUALIFICATION PROFILE PERSISTENCE
# ==============================================================================
def test_11_qualification_profiling_and_persistence(tmp_path: Path):
    """Verify ModelHardwareQualifier integration generates and persists valid profile."""
    provider = build_default_reasoning_provider(n_ctx=512)

    try:
        profile = qualify_local_reasoning_provider(provider, qualification_dir=tmp_path)
        assert isinstance(profile, ModelHardwareQualificationProfile)
        assert profile.model_id == "qwen35-4b-q4km"
        assert profile.qualification_status in ("QUALIFIED", "CONSTRAINED")
        assert profile.measured_p95_latency_ms > 0.0
        assert profile.measured_resident_ram_bytes > 0

        # Verify persisted JSON can be reloaded
        qualifier = ModelHardwareQualifier(qualification_dir=tmp_path)
        reloaded = qualifier.load_profile("qwen35-4b-q4km")
        assert reloaded is not None
        assert reloaded.hardware_hash == profile.hardware_hash
        assert reloaded.measured_resident_ram_bytes == profile.measured_resident_ram_bytes
    finally:
        provider.unload()
