"""Comprehensive Unit Tests for AEGIS Model Resource Policy & Skill Concurrency.

Covers:
- ModelRuntimeRegistry deduplication and inventory
- Exact manifest-pinned artifact sizes (3,168,262,223 bytes)
- Storage policy boundaries and host reserve floor (40 GB)
- Empirical hardware qualification (MEASURED_PROBE_LOAD_AND_LATENCY)
- ModelResidencyManager lease protection and non-eviction
- SkillConcurrencyManager dynamic admission, shared residency, and 2-3 skill target
- Heterogeneous execution (lightweight skills alongside heavyweight models)
- Configurable/qualification-derived safety reserves
- Provider integration with non-blocking admission and graceful fallback
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.decision.models import (
    DecisionOutcome,
    DecisionRoute,
    FallbackReason,
    FastDecisionRequest,
)
from src.decision.providers.von import VonFastDecisionProvider
from src.decision.providers.von_runtime import (
    SKILL_LIGHTWEIGHT_VALIDATOR,
    SKILL_VON_ROUTING,
    TORCH_CUDA12_RUNTIME,
    VON_1_1_395M_MODEL,
    VON_EXACT_ARTIFACT_BYTES,
    VON_FILES_BY_SIZE,
    build_default_model_registry,
)
from src.model_policy.concurrency_manager import SkillConcurrencyManager
from src.model_policy.live_benchmark import LiveHardwareBenchmark
from src.model_policy.models import (
    ModelArtifactDescriptor,
    ModelHardwareQualificationProfile,
    SkillResourceDescriptor,
)
from src.model_policy.qualification import ModelHardwareQualifier
from src.model_policy.residency_manager import ModelResidencyManager
from src.model_policy.runtime_registry import ModelRuntimeRegistry
from src.model_policy.storage_policy import ModelStoragePolicy


# ==============================================================================
# 1. RUNTIME REGISTRY & DEDUPLICATION
# ==============================================================================
def test_01_runtime_registry_deduplication():
    """Verify shared runtimes are counted once regardless of model count."""
    registry = ModelRuntimeRegistry()
    registry.register_runtime(TORCH_CUDA12_RUNTIME)

    # Register 2 models sharing torch-cuda12
    m1 = ModelArtifactDescriptor(
        model_id="model-1",
        manifest_name="manifest-1",
        runtime_id="torch-cuda12-2.6",
        exact_artifact_bytes=2_000_000_000,
        expected_sha256_hashes={},
    )
    m2 = ModelArtifactDescriptor(
        model_id="model-2",
        manifest_name="manifest-2",
        runtime_id="torch-cuda12-2.6",
        exact_artifact_bytes=3_000_000_000,
        expected_sha256_hashes={},
    )
    registry.register_model(m1)
    registry.register_model(m2)

    # Runtime disk footprint must be 4.8 GB (counted ONCE, not 9.6 GB)
    dedup_runtime = registry.calculate_deduplicated_runtime_footprint()
    assert dedup_runtime == TORCH_CUDA12_RUNTIME.installed_disk_bytes
    assert dedup_runtime == 4_800_000_000

    # Total artifact footprint is sum of models
    artifacts = registry.calculate_total_artifact_footprint()
    assert artifacts == 5_000_000_000


def test_02_runtime_registry_missing_runtime_fails():
    """Verify registering a model pointing to an unknown runtime fails with KeyError."""
    registry = ModelRuntimeRegistry()
    m = ModelArtifactDescriptor(
        model_id="unregistered-rt-model",
        manifest_name="manifest",
        runtime_id="non-existent-runtime",
        exact_artifact_bytes=1000,
        expected_sha256_hashes={},
    )
    with pytest.raises(KeyError) as exc_info:
        registry.register_model(m)
    assert "is not registered" in str(exc_info.value)


# ==============================================================================
# 2. EXACT MANIFEST-PINNED ARTIFACT SIZING
# ==============================================================================
def test_03_exact_manifest_pinned_bytes():
    """Verify Von artifact sizes match exact manifest-pinned byte count (3,168,262,223)."""
    assert sum(VON_FILES_BY_SIZE.values()) == VON_EXACT_ARTIFACT_BYTES
    assert VON_EXACT_ARTIFACT_BYTES == 3_168_262_223
    assert VON_1_1_395M_MODEL.exact_artifact_bytes == 3_168_262_223

    # Check key component file sizes
    assert VON_FILES_BY_SIZE["model.safetensors"] == 1_583_355_740
    assert VON_FILES_BY_SIZE["option_marker.pt"] == 1_581_316_050
    assert VON_FILES_BY_SIZE["tokenizer.json"] == 3_583_485


# ==============================================================================
# 3. STORAGE POLICY & BOUNDARIES
# ==============================================================================
def test_04_storage_policy_boundary_validation(tmp_path: Path):
    """Verify storage boundary refuses paths inside OneDrive or repo."""
    registry = build_default_model_registry()
    policy = ModelStoragePolicy(registry=registry, storage_root=tmp_path / "valid_models")
    assert policy.validate_storage_boundary(tmp_path / "valid_models") is True

    # OneDrive violation
    onedrive_path = Path("C:/Users/test/OneDrive/models")
    assert policy.validate_storage_boundary(onedrive_path) is False


def test_05_storage_preflight_passes_with_headroom(tmp_path: Path):
    """Verify preflight succeeds when free disk and budget are sufficient."""
    registry = build_default_model_registry()
    policy = ModelStoragePolicy(
        registry=registry,
        min_host_reserve_bytes=40 * 1024 * 1024 * 1024,
        model_storage_budget_bytes=30 * 1024 * 1024 * 1024,
        storage_root=tmp_path / "models",
    )

    # 84 GB free space, installing Von + torch-cuda12 (~11 GB transient needed)
    res = policy.preflight_model_installation(
        model_id="von-1.1-395m",
        mock_host_free_bytes=84 * 1024 * 1024 * 1024,
    )
    assert res.permitted is True
    assert "Preflight passed" in res.reason


def test_06_storage_preflight_blocks_below_host_floor(tmp_path: Path):
    """Verify preflight rejects installation that would violate 40 GB host reserve floor."""
    registry = build_default_model_registry()
    policy = ModelStoragePolicy(
        registry=registry,
        min_host_reserve_bytes=40 * 1024 * 1024 * 1024,
        model_storage_budget_bytes=30 * 1024 * 1024 * 1024,
        storage_root=tmp_path / "models",
    )

    # 44 GB free space: 44 GB - ~11 GB transient = 33 GB < 40 GB floor
    res = policy.preflight_model_installation(
        model_id="von-1.1-395m",
        mock_host_free_bytes=44 * 1024 * 1024 * 1024,
    )
    assert res.permitted is False
    assert "Host storage floor violation" in res.reason


def test_07_storage_preflight_blocks_budget_exhaustion(tmp_path: Path):
    """Verify preflight rejects installation exceeding model storage budget."""
    registry = build_default_model_registry()
    # Budget set to only 5 GB (Von + PyTorch needs ~7.97 GB permanent)
    policy = ModelStoragePolicy(
        registry=registry,
        min_host_reserve_bytes=10 * 1024 * 1024 * 1024,
        model_storage_budget_bytes=5 * 1024 * 1024 * 1024,
        storage_root=tmp_path / "models",
    )
    res = policy.preflight_model_installation(
        model_id="von-1.1-395m",
        mock_host_free_bytes=100 * 1024 * 1024 * 1024,
    )
    assert res.permitted is False
    assert "Global model budget violation" in res.reason


# ==============================================================================
# 4. HARDWARE QUALIFICATION & MEASUREMENT
# ==============================================================================
def test_08_live_benchmark_query_memory():
    """Verify LiveHardwareBenchmark returns real non-zero memory stats."""
    benchmark = LiveHardwareBenchmark()
    avail_ram, total_ram, free_vram, total_vram = benchmark.query_live_memory()
    assert avail_ram > 0
    assert total_ram >= avail_ram
    assert free_vram > 0
    assert total_vram >= free_vram


def test_09_qualification_derives_resident_and_marginal_memory(tmp_path: Path):
    """Verify hardware qualification executes probes and records empirical metrics."""
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=8 * 1024 * 1024 * 1024,
        mock_total_ram_bytes=16 * 1024 * 1024 * 1024,
        mock_free_vram_bytes=5 * 1024 * 1024 * 1024,
        mock_total_vram_bytes=6 * 1024 * 1024 * 1024,
    )
    qualifier = ModelHardwareQualifier(benchmark=benchmark, qualification_dir=tmp_path)

    # Simulated load consumes 2.5 GB RAM, 1.5 GB VRAM
    def mock_load():
        benchmark.set_mock_memory(
            avail_ram_bytes=int(5.5 * 1024 * 1024 * 1024),
            free_vram_bytes=int(3.5 * 1024 * 1024 * 1024),
        )

    # Simulated inference consumes 64 MB marginal RAM, 32 MB VRAM
    def mock_infer():
        benchmark.set_mock_memory(
            avail_ram_bytes=int(5.5 * 1024 * 1024 * 1024 - 64 * 1024 * 1024),
            free_vram_bytes=int(3.5 * 1024 * 1024 * 1024 - 32 * 1024 * 1024),
        )
        return 12.5

    profile = qualifier.qualify_model(
        model_id="von-1.1-395m",
        manifest_name="von-1.1-option-marker-395m",
        probe_load_fn=mock_load,
        probe_inference_fn=mock_infer,
    )

    assert profile.model_id == "von-1.1-395m"
    assert profile.measured_resident_ram_bytes == int(2.5 * 1024 * 1024 * 1024)
    assert profile.measured_resident_vram_bytes == int(1.5 * 1024 * 1024 * 1024)
    assert profile.measured_marginal_ram_bytes >= 64 * 1024 * 1024
    assert profile.measured_marginal_vram_bytes >= 32 * 1024 * 1024
    assert profile.qualification_status == "QUALIFIED"

    # Reload from disk
    loaded = qualifier.load_profile("von-1.1-395m")
    assert loaded is not None
    assert loaded.hardware_hash == profile.hardware_hash


# ==============================================================================
# 5. MODEL RESIDENCY & LEASE PROTECTION
# ==============================================================================
def test_10_residency_manager_lifecycle():
    """Verify ModelResidencyManager tracks resident models accurately."""
    mgr = ModelResidencyManager()
    assert mgr.is_resident("von-1.1-395m") is False
    mgr.set_resident("von-1.1-395m", True)
    assert mgr.is_resident("von-1.1-395m") is True
    assert mgr.can_unload("von-1.1-395m") is True
    assert mgr.unload_model("von-1.1-395m") is True
    assert mgr.is_resident("von-1.1-395m") is False


def test_11_residency_manager_protects_active_leases():
    """Verify model CANNOT be evicted while active skill leases are attached."""
    mgr = ModelResidencyManager()
    mgr.set_resident("von-1.1-395m", True)

    # Attach active lease
    mgr.attach_skill_lease("von-1.1-395m", "lease-101")
    assert mgr.has_active_leases("von-1.1-395m") is True
    assert mgr.can_unload("von-1.1-395m") is False

    # Attempting to unload fails safely
    assert mgr.unload_model("von-1.1-395m") is False
    with pytest.raises(RuntimeError) as exc_info:
        mgr.set_resident("von-1.1-395m", False)
    assert "Cannot unload model" in str(exc_info.value)

    # Detach lease and verify unload is now permitted
    mgr.detach_skill_lease("von-1.1-395m", "lease-101")
    assert mgr.can_unload("von-1.1-395m") is True
    assert mgr.unload_model("von-1.1-395m") is True


# ==============================================================================
# 6. DYNAMIC SKILL CONCURRENCY & SHARED RESIDENCY
# ==============================================================================
def test_12_shared_model_multiple_skills_concurrency():
    """Verify multiple concurrent skills share a single resident model instance."""
    res_mgr = ModelResidencyManager()
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=6 * 1024 * 1024 * 1024,
        mock_free_vram_bytes=4 * 1024 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
        system_ram_reserve_bytes=1536 * 1024 * 1024,
        target_concurrent_skills=3,
    )

    # Register qualification profile
    qual = ModelHardwareQualificationProfile(
        model_id="von-1.1-395m",
        manifest_name="von-1.1-option-marker-395m",
        hardware_hash="mock-hash",
        measured_resident_ram_bytes=2000 * 1024 * 1024,  # 2.0 GB
        measured_resident_vram_bytes=1500 * 1024 * 1024,  # 1.5 GB
        measured_marginal_ram_bytes=64 * 1024 * 1024,
        measured_marginal_vram_bytes=32 * 1024 * 1024,
        measured_p95_latency_ms=10.0,
    )
    concurrency_mgr.register_qualification_profile(qual)

    # Skill 1 acquires lease
    skill_desc_1 = SkillResourceDescriptor(
        skill_id="intent_triage", required_model_id="von-1.1-395m"
    )
    lease_1 = concurrency_mgr.acquire_skill_lease(skill_desc_1)
    assert lease_1 is not None
    assert res_mgr.is_resident("von-1.1-395m") is True
    assert res_mgr.get_active_lease_count("von-1.1-395m") == 1

    # Skill 2 acquires lease sharing the SAME resident model
    skill_desc_2 = SkillResourceDescriptor(
        skill_id="policy_router", required_model_id="von-1.1-395m"
    )
    lease_2 = concurrency_mgr.acquire_skill_lease(skill_desc_2)
    assert lease_2 is not None
    assert res_mgr.get_active_lease_count("von-1.1-395m") == 2
    assert len(res_mgr.list_resident_models()) == 1  # Only ONE resident model in RAM/VRAM

    # Release both
    concurrency_mgr.release_skill_lease(lease_1)
    assert res_mgr.get_active_lease_count("von-1.1-395m") == 1
    concurrency_mgr.release_skill_lease(lease_2)
    assert res_mgr.get_active_lease_count("von-1.1-395m") == 0


def test_13_heterogeneous_lightweight_and_heavyweight_skills():
    """Verify lightweight non-model skills run concurrently with heavy ML model."""
    res_mgr = ModelResidencyManager()
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=6 * 1024 * 1024 * 1024,
        mock_free_vram_bytes=4 * 1024 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
        system_ram_reserve_bytes=1536 * 1024 * 1024,
        target_concurrent_skills=3,
    )

    # 1 Heavy ML skill (Von)
    lease_heavy = concurrency_mgr.acquire_skill_lease(SKILL_VON_ROUTING)
    assert lease_heavy is not None

    # 2 Lightweight CPU skills (Validator, Sanitizer)
    lease_light_1 = concurrency_mgr.acquire_skill_lease(SKILL_LIGHTWEIGHT_VALIDATOR)
    assert lease_light_1 is not None

    skill_light_2 = SkillResourceDescriptor(
        skill_id="context_sanitizer",
        required_model_id=None,
        declared_marginal_ram_bytes=16 * 1024 * 1024,
    )
    lease_light_2 = concurrency_mgr.acquire_skill_lease(skill_light_2)
    assert lease_light_2 is not None

    # Total active concurrent skills = 3 (target satisfied!)
    assert concurrency_mgr.get_active_skill_count() == 3

    # Clean up
    concurrency_mgr.release_skill_lease(lease_heavy)
    concurrency_mgr.release_skill_lease(lease_light_1)
    concurrency_mgr.release_skill_lease(lease_light_2)
    assert concurrency_mgr.get_active_skill_count() == 0


def test_14_concurrency_ceiling_enforced():
    """Verify 4th skill acquisition is refused when target concurrency is 3."""
    res_mgr = ModelResidencyManager()
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=8 * 1024 * 1024 * 1024,
        mock_free_vram_bytes=5 * 1024 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
        target_concurrent_skills=3,
    )

    l1 = concurrency_mgr.acquire_skill_lease(SKILL_LIGHTWEIGHT_VALIDATOR)
    l2 = concurrency_mgr.acquire_skill_lease(SKILL_LIGHTWEIGHT_VALIDATOR)
    l3 = concurrency_mgr.acquire_skill_lease(SKILL_LIGHTWEIGHT_VALIDATOR)
    assert l1 and l2 and l3

    # 4th skill must be refused
    l4 = concurrency_mgr.acquire_skill_lease(SKILL_LIGHTWEIGHT_VALIDATOR)
    assert l4 is None

    eval_res = concurrency_mgr.evaluate_skill_admission(SKILL_LIGHTWEIGHT_VALIDATOR)
    assert eval_res.permitted is False
    assert "reached target ceiling (3)" in eval_res.reason

    concurrency_mgr.release_skill_lease(l1)
    concurrency_mgr.release_skill_lease(l2)
    concurrency_mgr.release_skill_lease(l3)


def test_15_dynamic_memory_gate_blocks_heavy_model_under_3gb_ram():
    """Verify that with 3.0 GB free RAM and 3.2 GB resident model, ML skill is refused."""
    res_mgr = ModelResidencyManager()
    # Host under current pressure: 3.0 GB free RAM
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=3000 * 1024 * 1024,
        mock_free_vram_bytes=5900 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
        system_ram_reserve_bytes=1536 * 1024 * 1024,  # 1.5 GB reserve
    )

    # Register qualification with 3.2 GB resident RAM
    qual = ModelHardwareQualificationProfile(
        model_id="von-1.1-395m",
        manifest_name="von-1.1-option-marker-395m",
        hardware_hash="mock-hash",
        measured_resident_ram_bytes=3200 * 1024 * 1024,  # 3.2 GB resident
        measured_resident_vram_bytes=1600 * 1024 * 1024,
        measured_marginal_ram_bytes=64 * 1024 * 1024,
        measured_marginal_vram_bytes=32 * 1024 * 1024,
        measured_p95_latency_ms=12.0,
    )
    concurrency_mgr.register_qualification_profile(qual)

    # Attempt ML skill lease: Headroom is 3.0 GB - 1.5 GB = 1.5 GB < 3.2 GB needed
    lease = concurrency_mgr.acquire_skill_lease(SKILL_VON_ROUTING)
    assert lease is None

    eval_res = concurrency_mgr.evaluate_skill_admission(SKILL_VON_ROUTING)
    assert eval_res.permitted is False
    assert "RAM memory gate" in eval_res.reason


def test_16_configurable_safety_reserves():
    """Verify safety reserves can be dynamically updated via configuration."""
    res_mgr = ModelResidencyManager()
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=4000 * 1024 * 1024,
        mock_free_vram_bytes=5000 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
        system_ram_reserve_bytes=1536 * 1024 * 1024,
    )

    concurrency_mgr.update_safety_reserves(
        system_ram_reserve_bytes=500 * 1024 * 1024,
        target_concurrent_skills=5,
    )
    assert concurrency_mgr.system_ram_reserve_bytes == 500 * 1024 * 1024
    assert concurrency_mgr.target_concurrent_skills == 5


def test_17_lightweight_skill_permitted_even_when_heavy_blocked():
    """Verify lightweight skills succeed under 3.0 GB free RAM when heavy ML is blocked."""
    res_mgr = ModelResidencyManager()
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=3000 * 1024 * 1024,
        mock_free_vram_bytes=5900 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
        system_ram_reserve_bytes=1536 * 1024 * 1024,
    )

    # Lightweight skill requires only 24 MB -> fits in 1.46 GB headroom!
    lease = concurrency_mgr.acquire_skill_lease(SKILL_LIGHTWEIGHT_VALIDATOR)
    assert lease is not None
    assert lease.skill_id == "lightweight_rule_validator"
    concurrency_mgr.release_skill_lease(lease)


# ==============================================================================
# 7. PROVIDER INTEGRATION & FALLBACK
# ==============================================================================
def test_18_von_provider_integrates_concurrency_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify VonFastDecisionProvider acquires and releases lease during evaluate()."""
    res_mgr = ModelResidencyManager()
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=8 * 1024 * 1024 * 1024,
        mock_free_vram_bytes=5 * 1024 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
    )

    # Mock backend
    mock_backend = MagicMock()
    mock_backend.decide.return_value = {
        "choice": "terminal_intent",
        "confidence": 0.95,
        "scores": {
            "terminal_intent": 0.95,
            "reasoning_intent": 0.05,
        },
    }

    checkpoint = tmp_path / "mock_von"
    checkpoint.mkdir()

    provider = VonFastDecisionProvider(
        checkpoint_dir=checkpoint,
        manifest_path=None,
        backend_factory=lambda p: mock_backend,
        concurrency_manager=concurrency_mgr,
    )

    # Mock integrity & smoke
    monkeypatch.setattr(provider, "verify_integrity", MagicMock(return_value=True))
    monkeypatch.setattr(provider, "validate_storage_boundaries", MagicMock(return_value=None))
    monkeypatch.setattr(provider, "_run_smoke_inference", MagicMock(return_value=True))
    assert provider.initialize() is True

    # Pre-evaluate lease count is 0
    assert concurrency_mgr.get_active_skill_count() == 0

    req = FastDecisionRequest(user_input="Restart the server immediately.")
    criteria = {
        DecisionRoute.TERMINAL_INTENT: "terminal intent",
        DecisionRoute.REASONING_INTENT: "reasoning intent",
    }

    res = provider.evaluate(req, criteria)
    assert res.outcome == DecisionOutcome.FAST_PATH
    assert res.route == DecisionRoute.TERMINAL_INTENT

    # Post-evaluate lease count is restored to 0
    assert concurrency_mgr.get_active_skill_count() == 0


def test_19_von_provider_falls_back_when_concurrency_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify VonFastDecisionProvider returns fallback ADMISSION_BUSY when concurrency denied."""
    res_mgr = ModelResidencyManager()
    # Free RAM too low: 1.5 GB free with 1.5 GB reserve -> 0 headroom
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=1500 * 1024 * 1024,
        mock_free_vram_bytes=5000 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
        system_ram_reserve_bytes=1536 * 1024 * 1024,
    )

    mock_backend = MagicMock()
    checkpoint = tmp_path / "mock_von"
    checkpoint.mkdir()

    provider = VonFastDecisionProvider(
        checkpoint_dir=checkpoint,
        manifest_path=None,
        backend_factory=lambda p: mock_backend,
        concurrency_manager=concurrency_mgr,
    )

    monkeypatch.setattr(provider, "verify_integrity", MagicMock(return_value=True))
    monkeypatch.setattr(provider, "validate_storage_boundaries", MagicMock(return_value=None))
    monkeypatch.setattr(provider, "_run_smoke_inference", MagicMock(return_value=True))
    assert provider.initialize() is True

    req = FastDecisionRequest(user_input="Check log status.")
    criteria = {DecisionRoute.TERMINAL_INTENT: "terminal intent"}

    # Admission denied -> instant non-blocking fallback
    res = provider.evaluate(req, criteria)
    assert res.outcome == DecisionOutcome.FALLBACK_TO_MANAGER
    assert res.fallback_reason == FallbackReason.ADMISSION_BUSY
    assert res.is_advisory_only is True


def test_20_concurrency_manager_release_frees_capacity():
    """Verify releasing leases correctly restores capacity."""
    res_mgr = ModelResidencyManager()
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=8 * 1024 * 1024 * 1024,
        mock_free_vram_bytes=5 * 1024 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
        target_concurrent_skills=1,
    )

    lease = concurrency_mgr.acquire_skill_lease(SKILL_LIGHTWEIGHT_VALIDATOR)
    assert lease is not None
    assert concurrency_mgr.get_active_skill_count() == 1

    # Second acquisition fails (limit=1)
    assert concurrency_mgr.acquire_skill_lease(SKILL_LIGHTWEIGHT_VALIDATOR) is None

    # Release
    concurrency_mgr.release_skill_lease(lease)
    assert concurrency_mgr.get_active_skill_count() == 0

    # Acquisition now succeeds again
    lease2 = concurrency_mgr.acquire_skill_lease(SKILL_LIGHTWEIGHT_VALIDATOR)
    assert lease2 is not None
    concurrency_mgr.release_skill_lease(lease2)


def test_21_dynamic_vram_gate_blocks_heavy_model_under_insufficient_vram():
    """Verify dynamic VRAM gate blocks heavy model when VRAM headroom is insufficient."""
    res_mgr = ModelResidencyManager()
    # High RAM (8 GB), but free VRAM is 600 MB with 512 MB reserve -> only 88 MB headroom
    # Heavy model uncalibrated requires 1600 MB resident + 32 MB marginal VRAM = 1632 MB needed
    benchmark = LiveHardwareBenchmark(
        mock_avail_ram_bytes=8 * 1024 * 1024 * 1024,
        mock_free_vram_bytes=600 * 1024 * 1024,
    )
    concurrency_mgr = SkillConcurrencyManager(
        residency_manager=res_mgr,
        benchmark=benchmark,
        system_vram_reserve_bytes=512 * 1024 * 1024,
    )

    eval_result = concurrency_mgr.evaluate_skill_admission(SKILL_VON_ROUTING)
    assert eval_result.permitted is False
    assert "VRAM memory gate" in eval_result.reason
    assert eval_result.max_safe_skills == 0
