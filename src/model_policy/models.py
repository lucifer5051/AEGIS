"""AEGIS Model Resource Policy & Skill Concurrency - Data Models.

Defines typed schemas for runtimes, model artifacts, empirical hardware qualification,
per-skill resource descriptors, active skill execution leases, and storage budgeting.
All models use Pydantic v2 with extra='forbid' and deterministic defaults.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class RuntimeFamily(str, Enum):
    """Supported framework/runtime execution families."""

    PYTORCH_CUDA = "pytorch-cuda"
    PYTORCH_CPU = "pytorch-cpu"
    ONNX_RUNTIME = "onnxruntime"
    LLAMACPP = "llamacpp"
    CUSTOM = "custom"


class ConcurrencyMode(str, Enum):
    """Dynamic concurrency mode determined by live hardware benchmarking."""

    OPTIMAL = "OPTIMAL"          # High headroom: target concurrent skills fully enabled
    NORMAL = "NORMAL"            # Normal headroom: target concurrent skills enabled
    CONSTRAINED = "CONSTRAINED"  # Low headroom: shared residency or lightweight skills only
    CRITICAL = "CRITICAL"        # Severe pressure: emergency single skill / advisory bypass


class ModelRuntimeDescriptor(BaseModel):
    """Specification of an installable framework runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: str = Field(description="Unique runtime identifier, e.g. 'torch-cuda12-2.6'.")
    runtime_family: RuntimeFamily = Field(description="Runtime execution framework family.")
    version: str = Field(description="Framework version string.")
    package_dependencies: List[str] = Field(
        default_factory=list, description="Direct package dependencies required by this runtime."
    )
    installed_disk_bytes: int = Field(
        ge=0, description="Installed disk footprint on host filesystem."
    )
    transient_install_bytes: int = Field(
        ge=0, description="Peak temporary disk space required during download/wheel extraction."
    )
    cuda_capable: bool = Field(default=False, description="Whether this runtime utilizes CUDA.")
    shared_runtime: bool = Field(
        default=True, description="Whether this runtime can be shared by multiple models."
    )


class ModelArtifactDescriptor(BaseModel):
    """Specification of a pinned model artifact on disk."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = Field(description="Canonical model identifier, e.g. 'von-1.1-395m'.")
    manifest_name: str = Field(description="Name of associated trusted manifest file.")
    runtime_id: str = Field(description="Identifier of required ModelRuntimeDescriptor.")
    exact_artifact_bytes: int = Field(
        ge=1, description="Exact sum of manifest-pinned artifact file sizes on disk."
    )
    expected_sha256_hashes: Dict[str, str] = Field(
        description="Map of relative artifact filenames to verified SHA-256 hashes."
    )
    context_window_tokens: int = Field(
        default=2048, ge=64, description="Maximum supported context length."
    )


class ModelHardwareQualificationProfile(BaseModel):
    """Empirically measured hardware qualification metrics for a model on this host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = Field(description="Identifier of qualified model.")
    manifest_name: str = Field(
        default="unknown", description="Name of associated trusted manifest file."
    )
    measured_at_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 timestamp of measurement probe.",
    )
    hardware_hash: str = Field(description="Digest of system CPU, GPU, and RAM topology.")
    measured_resident_ram_bytes: int = Field(
        ge=0, description="Empirically measured delta in process RAM after model load."
    )
    measured_resident_vram_bytes: int = Field(
        ge=0, description="Empirically measured delta in GPU VRAM after model load."
    )
    measured_marginal_ram_bytes: int = Field(
        ge=0, description="Empirically measured marginal RAM consumed per concurrent inference."
    )
    measured_marginal_vram_bytes: int = Field(
        ge=0, description="Empirically measured marginal VRAM consumed per concurrent inference."
    )
    measured_p95_latency_ms: float = Field(
        ge=0.0, description="Empirically measured 95th percentile inference latency."
    )
    qualification_status: Literal["QUALIFIED", "CONSTRAINED", "FAILED"] = Field(
        default="QUALIFIED", description="Qualification status."
    )


class SkillResourceDescriptor(BaseModel):
    """Declared or profiled resource footprint for an executing skill."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(description="Unique skill capability identifier.")
    required_model_id: Optional[str] = Field(
        default=None, description="Model ID required, or None for lightweight non-model skills."
    )
    declared_marginal_ram_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=0,
        description="Marginal system RAM required during skill execution.",
    )
    declared_marginal_vram_bytes: int = Field(
        default=0,
        ge=0,
        description="Marginal GPU VRAM required during skill execution (0 for CPU skills).",
    )
    marginal_cpu_cores: float = Field(
        default=1.0, ge=0.1, le=32.0, description="CPU thread compute footprint."
    )
    timeout_ms: int = Field(
        default=500, ge=50, le=30000, description="Max acceptable lease wait timeout in ms."
    )
    priority: int = Field(
        default=100, ge=1, le=1000, description="Priority rank (lower number = higher priority)."
    )


class SkillExecutionLease(BaseModel):
    """Active reservation lease granted to an executing skill."""

    model_config = ConfigDict(extra="forbid")

    lease_id: str = Field(description="Unique lease UUID.")
    skill_id: str = Field(description="Identifier of the executing skill.")
    model_id: Optional[str] = Field(
        default=None, description="Resident model bound to this lease, if any."
    )
    reserved_ram_bytes: int = Field(ge=0, description="RAM reserved for this skill execution.")
    reserved_vram_bytes: int = Field(ge=0, description="VRAM reserved for this skill execution.")
    granted_at_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Lease grant timestamp.",
    )
    is_active: bool = Field(default=True, description="Whether the lease is currently in-flight.")


class ConcurrencyEvaluation(BaseModel):
    """Admission evaluation result for a requested skill lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    permitted: bool = Field(description="Whether the skill execution lease is granted.")
    active_skill_count: int = Field(ge=0, description="Count of active skills currently leased.")
    max_safe_skills: int = Field(ge=0, description="Current dynamic safe skill concurrency limit.")
    mode: ConcurrencyMode = Field(description="Dynamic concurrency mode at evaluation time.")
    reason: str = Field(description="Human-readable rationale for the admission decision.")
    measured_free_ram_bytes: int = Field(
        ge=0, description="Live measured free RAM at evaluation."
    )
    measured_free_vram_bytes: int = Field(
        ge=0, description="Live measured free VRAM at evaluation."
    )


class PreflightResult(BaseModel):
    """Storage pre-flight verification outcome before installation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    permitted: bool = Field(description="Whether installation satisfies all storage boundaries.")
    required_bytes: int = Field(ge=0, description="Total storage required (model + new runtime).")
    transient_bytes: int = Field(ge=0, description="Peak transient installation headroom needed.")
    free_storage_bytes: int = Field(ge=0, description="Host storage free bytes before operation.")
    storage_floor_bytes: int = Field(ge=0, description="Configured host minimum storage floor.")
    reason: str = Field(description="Preflight outcome description.")


class StorageBudgetStatus(BaseModel):
    """Summary of global model and runtime storage budget."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_budget_bytes: int = Field(ge=0, description="Configured maximum model storage budget.")
    consumed_runtime_bytes: int = Field(
        ge=0, description="Deduplicated disk footprint of installed runtimes."
    )
    consumed_artifact_bytes: int = Field(
        ge=0, description="Sum of installed model artifact sizes."
    )
    remaining_budget_bytes: int = Field(
        ge=0, description="Available capacity within the model storage budget."
    )
    host_free_bytes: int = Field(ge=0, description="Actual physical free space on host volume.")
    host_reserve_floor_bytes: int = Field(ge=0, description="Hard host reserve floor.")
