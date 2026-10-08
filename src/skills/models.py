"""SAGE Skills Subsystem -- Immutable Domain Models.

Defines declarative SkillManifest, SkillInvocation contract, SkillResult,
and typed enumerations for skill lifecycle, execution strategies, and policies.

Invariants:
- All models are immutable (frozen=True, extra="forbid").
- Skills cannot define or alter Trust Core policies.
- Resource requirements and tool bindings are statically declared.
"""

from __future__ import annotations

from enum import Enum, unique
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.trust.capability import ActionRisk
from src.trust.policy import AutonomyTier

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


@unique
class SkillType(str, Enum):
    """Execution paradigm and complexity classification for skills."""

    DETERMINISTIC = "DETERMINISTIC"
    REASONING = "REASONING"
    WORKFLOW = "WORKFLOW"
    COMPOSITE = "COMPOSITE"


@unique
class SkillLifecycleState(str, Enum):
    """Deterministic lifecycle state machine for skills."""

    DISCOVERED = "DISCOVERED"
    VALIDATING = "VALIDATING"
    VERIFIED = "VERIFIED"
    REGISTERED = "REGISTERED"
    AVAILABLE = "AVAILABLE"
    SELECTED = "SELECTED"
    ADMITTED = "ADMITTED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RECOVERY = "RECOVERY"


@unique
class ExecutionStrategy(str, Enum):
    """Strategy for orchestrating skill execution."""

    DIRECT = "DIRECT"
    STEPPED = "STEPPED"
    DELEGATED = "DELEGATED"
    PIPELINE = "PIPELINE"


@unique
class MemoryAccessPolicy(str, Enum):
    """Permissions for accessing long-term and episodic memory."""

    NONE = "NONE"
    READ_ONLY = "READ_ONLY"
    EPISODIC = "EPISODIC"
    FULL = "FULL"


@unique
class ProjectScope(str, Enum):
    """Boundary of access within the project hierarchy."""

    GLOBAL = "GLOBAL"
    PROJECT_BOUND = "PROJECT_BOUND"
    WORKSPACE_ONLY = "WORKSPACE_ONLY"


@unique
class SkillVerificationPolicy(str, Enum):
    """Verification mechanism applied to skill execution output."""

    DETERMINISTIC_ASSERTION = "DETERMINISTIC_ASSERTION"
    SCHEMA_VALIDATION = "SCHEMA_VALIDATION"
    HUMAN_IN_THE_LOOP = "HUMAN_IN_THE_LOOP"
    NONE = "NONE"


@unique
class SkillRecoveryPolicy(str, Enum):
    """Recovery strategy invoked on step or execution failure."""

    RETRY_IDEMPOTENT = "RETRY_IDEMPOTENT"
    ROLLBACK_CHECKPOINT = "ROLLBACK_CHECKPOINT"
    ESCALATE = "ESCALATE"
    FAIL_FAST = "FAIL_FAST"


@unique
class SkillExecutionStatus(str, Enum):
    """Terminal or operational status of a skill execution."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ESCALATED = "ESCALATED"
    DEFERRED = "DEFERRED"


@unique
class VerificationStatus(str, Enum):
    """Status resulting from post-execution verification."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


# ---------------------------------------------------------------------------
# Declarative Resource & Concurrency Descriptors
# ---------------------------------------------------------------------------


class SkillResourceRequirement(BaseModel):
    """Declared resource footprint required by an executing skill.

    Note: The skill only declares requirements; the deterministic
    SkillAdmissionController evaluates and grants leases.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    declared_marginal_ram_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=0,
        description="Marginal system RAM required during skill execution.",
    )
    declared_marginal_vram_bytes: int = Field(
        default=0,
        ge=0,
        description="Marginal GPU VRAM required during skill execution.",
    )
    marginal_cpu_cores: float = Field(
        default=1.0,
        ge=0.1,
        le=32.0,
        description="CPU compute thread requirement.",
    )
    required_model_id: Optional[str] = Field(
        default=None,
        description="Pinned model ID required, or None for lightweight non-model skills.",
    )
    timeout_ms: int = Field(
        default=5000,
        ge=50,
        le=300000,
        description="Maximum execution timeout in milliseconds.",
    )
    concurrency_class: str = Field(
        default="standard",
        description="Concurrency scheduling class identifier.",
    )
    priority: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Scheduling priority rank (lower = higher priority).",
    )


class ConcurrencyPolicy(BaseModel):
    """Concurrency boundaries governing skill execution instances."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    concurrency_class: str = Field(
        default="standard",
        description="Concurrency scheduling class.",
    )
    max_concurrent: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Maximum simultaneous instances of this skill.",
    )
    allow_parallel: bool = Field(
        default=False,
        description="Whether multiple invocations may execute concurrently.",
    )


# ---------------------------------------------------------------------------
# Declarative Skill Manifest
# ---------------------------------------------------------------------------


class SkillManifest(BaseModel):
    """Immutable, declarative manifest describing a SAGE skill.

    Invariants:
    - Statically declares tool bindings and required capabilities.
    - Cannot define or modify Trust Core policy.
    - Pure data definition; no executable runtime state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(
        description="Unique canonical identifier (e.g. 'fs.file_writer', 'text.summarizer')."
    )
    name: str = Field(description="Human-readable name of the skill.")
    version: str = Field(description="Semver-compliant version string.")
    description: str = Field(description="Detailed description of capability and purpose.")
    skill_type: SkillType = Field(description="Execution paradigm classification.")
    intent_tags: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Intent tags for deterministic matching and discovery.",
    )
    input_schema: Dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema dictionary validating input arguments.",
    )
    output_schema: Dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema dictionary validating skill results.",
    )
    execution_strategy: ExecutionStrategy = Field(
        default=ExecutionStrategy.DIRECT,
        description="Strategy for executing the skill.",
    )
    tool_bindings: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Exact tool names this skill is permitted to access via Tool Broker.",
    )
    required_capabilities: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Canonical Trust Core action names required (e.g. 'filesystem.write').",
    )
    resource_requirements: SkillResourceRequirement = Field(
        default_factory=SkillResourceRequirement,
        description="Declared compute, memory, and model requirements.",
    )
    concurrency_policy: ConcurrencyPolicy = Field(
        default_factory=ConcurrencyPolicy,
        description="Concurrency boundaries governing execution.",
    )
    memory_access: MemoryAccessPolicy = Field(
        default=MemoryAccessPolicy.NONE,
        description="Permitted level of memory store access.",
    )
    project_scope: ProjectScope = Field(
        default=ProjectScope.WORKSPACE_ONLY,
        description="Scope of filesystem/project boundaries.",
    )
    risk_class: ActionRisk = Field(
        default=ActionRisk.LOW,
        description="Deterministic risk classification per Trust Core criteria.",
    )
    verification_policy: SkillVerificationPolicy = Field(
        default=SkillVerificationPolicy.SCHEMA_VALIDATION,
        description="Policy applied to verify skill execution outputs.",
    )
    recovery_policy: SkillRecoveryPolicy = Field(
        default=SkillRecoveryPolicy.FAIL_FAST,
        description="Recovery strategy applied upon failure.",
    )
    lifecycle_state: SkillLifecycleState = Field(
        default=SkillLifecycleState.DISCOVERED,
        description="Current lifecycle state of the manifest.",
    )


class RegisteredSkill(BaseModel):
    """Immutable record of an admitted and registered skill."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: SkillManifest = Field(description="Admitted declarative manifest.")
    registered_at_utc: str = Field(description="ISO 8601 registration timestamp.")
    current_state: SkillLifecycleState = Field(
        default=SkillLifecycleState.AVAILABLE,
        description="Current operational lifecycle state.",
    )


# ---------------------------------------------------------------------------
# Invocation & Result Contracts
# ---------------------------------------------------------------------------


class SkillInvocation(BaseModel):
    """Structured contract passed to execute a registered skill.

    Never contains raw credentials or unredacted secrets.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique invocation identifier.",
    )
    skill_id: str = Field(description="Target skill identifier.")
    task_id: Optional[str] = Field(
        default=None,
        description="Associated supervisor task ID if executing within a managed task.",
    )
    input: Dict[str, Any] = Field(
        default_factory=dict,
        description="Input arguments conforming to manifest.input_schema.",
    )
    context_reference: Optional[str] = Field(
        default=None,
        description="Deterministic reference to execution context or ContextIR.",
    )
    requested_tools: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Tools requested for this specific invocation (must be subset of bindings).",
    )
    capability_requirements: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Declared capabilities required for this invocation.",
    )
    resource_requirements: SkillResourceRequirement = Field(
        default_factory=SkillResourceRequirement,
        description="Resource allocation requested for this invocation.",
    )
    deadline_ms: Optional[float] = Field(
        default=None,
        description="Deadline in milliseconds from start before timing out.",
    )
    autonomy_level: AutonomyTier = Field(
        default=AutonomyTier.STRICT_MANUAL,
        description="Autonomy tier under which this invocation executes.",
    )


class SkillResult(BaseModel):
    """Structured return contract produced by skill execution.

    Never contains raw credentials or unredacted secrets.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str = Field(description="Corresponding invocation identifier.")
    skill_id: str = Field(description="Executing skill identifier.")
    status: SkillExecutionStatus = Field(description="Execution outcome status.")
    output: Dict[str, Any] = Field(
        default_factory=dict,
        description="Result payload conforming to manifest.output_schema.",
    )
    observations: Tuple[Dict[str, Any], ...] = Field(
        default_factory=tuple,
        description="Observations recorded during execution steps.",
    )
    verification_status: VerificationStatus = Field(
        default=VerificationStatus.UNVERIFIED,
        description="Outcome of post-execution verification.",
    )
    error: Optional[str] = Field(
        default=None,
        description="Sanitized error description on failure.",
    )
    recovery_hint: Optional[str] = Field(
        default=None,
        description="Suggested recovery strategy or action hint.",
    )
    metrics: Dict[str, Any] = Field(
        default_factory=dict,
        description="Execution metrics (duration_ms, memory_bytes, steps).",
    )
    experience_event: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Serialized ExperienceEvent recorded for this execution, if any.",
    )
