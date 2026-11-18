"""SAGE Configuration & Policy System -- Domain Models.

Canonical Reference:
    PROMPT 069: SAGE Configuration & Policy System
    docs/specifications/AEGIS_CONFIGURATION_SUBSYSTEM.md

Defines typed Pydantic models for:
1. Autonomy Modes & Profiles (Dimension 1)
2. Security Policies & Resource Boundaries (Dimension 2)
3. Execution Profiles & Computational Weights (Dimension 3)
4. Personality Configuration (Independent dimension)
5. Configuration Hierarchy Layers & Provenance
6. Custom Modes (Reusable multi-dimensional bundles)
7. Policy Evaluation Requests & Structured Decisions
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.config.models import AutonomyTier, NetworkMode
from src.trust.audit import _check_no_secrets


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ==============================================================================
# DIMENSION 1: AUTONOMY MODE & PROFILES
# ==============================================================================


@unique
class AutonomyMode(str, Enum):
    """Canonical operational autonomy modes."""

    MANUAL = "MANUAL"
    ASSISTED = "ASSISTED"
    SUPERVISED = "SUPERVISED"
    AUTONOMOUS = "AUTONOMOUS"
    CUSTOM = "CUSTOM"


class AutonomyProfile(BaseModel):
    """Operational autonomy profile controlling how independently SAGE operates."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    mode: AutonomyMode = Field(
        default=AutonomyMode.ASSISTED,
        description="Active autonomy mode category.",
    )
    name: str = Field(default="Standard Assisted", description="Profile name.")
    tier: AutonomyTier = Field(
        default=AutonomyTier.TIER_1_SAFE_BOUNDED,
        description="Underlying TrustCore autonomy tier (0=Manual, 1=Assisted, 2=Supervised, 3=Autonomous).",
    )
    propose_only: bool = Field(
        default=False,
        description="If True, SAGE only proposes actions and cannot execute them autonomously.",
    )
    checkpoint_required_after_steps: Optional[int] = Field(
        default=None,
        ge=1,
        description="Number of autonomous steps permitted before requiring approval checkpoint.",
    )
    allowed_autonomous_risks: List[str] = Field(
        default_factory=lambda: ["LOW"],
        description="Action risks permitted for autonomous execution without immediate approval.",
    )
    require_approval_for_actions: List[str] = Field(
        default_factory=list,
        description="Explicit actions that always require human approval under this profile.",
    )
    allow_user_override: bool = Field(
        default=True,
        description="Whether user can temporarily elevate tier for a specific task.",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Custom autonomy configuration metadata."
    )

    @classmethod
    def builtin_manual(cls) -> AutonomyProfile:
        return cls(
            mode=AutonomyMode.MANUAL,
            name="Manual",
            tier=AutonomyTier.TIER_0_STRICT_MANUAL,
            propose_only=True,
            checkpoint_required_after_steps=1,
            allowed_autonomous_risks=[],
            require_approval_for_actions=["*"],
        )

    @classmethod
    def builtin_assisted(cls) -> AutonomyProfile:
        return cls(
            mode=AutonomyMode.ASSISTED,
            name="Assisted",
            tier=AutonomyTier.TIER_1_SAFE_BOUNDED,
            propose_only=False,
            checkpoint_required_after_steps=None,
            allowed_autonomous_risks=["LOW"],
            require_approval_for_actions=[
                "filesystem.write",
                "filesystem.delete",
                "shell.execute",
                "network.egress",
            ],
        )

    @classmethod
    def builtin_supervised(cls) -> AutonomyProfile:
        return cls(
            mode=AutonomyMode.SUPERVISED,
            name="Supervised",
            tier=AutonomyTier.TIER_2_SUPERVISED,
            propose_only=False,
            checkpoint_required_after_steps=5,
            allowed_autonomous_risks=["LOW", "MEDIUM"],
            require_approval_for_actions=["filesystem.delete", "shell.execute"],
        )

    @classmethod
    def builtin_autonomous(cls) -> AutonomyProfile:
        return cls(
            mode=AutonomyMode.AUTONOMOUS,
            name="Autonomous",
            tier=AutonomyTier.TIER_3_FULL_BOUNDED,
            propose_only=False,
            checkpoint_required_after_steps=10,
            allowed_autonomous_risks=["LOW", "MEDIUM", "HIGH"],
            require_approval_for_actions=["filesystem.delete_root", "system.shutdown"],
        )


# ==============================================================================
# DIMENSION 2: SECURITY POLICY (SUBORDINATE TO TRUSTCORE)
# ==============================================================================


class FilesystemPolicy(BaseModel):
    """Filesystem boundary policy."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    allowed_paths: List[str] = Field(
        default_factory=lambda: ["."],
        description="Whitelisted filesystem directories.",
    )
    read_only_paths: List[str] = Field(
        default_factory=list,
        description="Directories where only read operations are permitted.",
    )
    denied_paths: List[str] = Field(
        default_factory=lambda: [
            "C:\\Windows",
            "C:\\Program Files",
            ".aegis/keys",
            "/etc",
            "/var",
        ],
        description="Blacklisted directories that can NEVER be accessed.",
    )


class NetworkPolicy(BaseModel):
    """Network egress policy boundaries."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    mode: NetworkMode = Field(
        default=NetworkMode.LOCAL_ONLY,
        description="Network boundary mode.",
    )
    allowed_domains: List[str] = Field(
        default_factory=list,
        description="Explicitly whitelisted domains when online access is allowed.",
    )
    denied_domains: List[str] = Field(
        default_factory=list,
        description="Strictly denied domains.",
    )


class ShellPolicy(BaseModel):
    """Command-line and shell execution policy."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    enabled: bool = Field(default=False, description="Whether shell execution is allowed.")
    allowed_commands: List[str] = Field(
        default_factory=list,
        description="Explicitly whitelisted shell commands (if empty, depends on tool broker).",
    )
    denied_commands: List[str] = Field(
        default_factory=lambda: [
            "rmdir /s",
            "del /f",
            "format",
            "powershell -enc",
            "curl",
            "wget",
        ],
        description="Strictly forbidden shell commands.",
    )
    sandbox_required: bool = Field(
        default=True,
        description="Whether shell commands must run in a sandboxed subprocess.",
    )


class ToolPolicy(BaseModel):
    """Tool-level authorization policy."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    allowed_tools: Optional[List[str]] = Field(
        default=None,
        description="Explicit list of permitted tools. None means all non-denied tools.",
    )
    denied_tools: List[str] = Field(
        default_factory=list,
        description="Explicitly denied tools.",
    )


class CommunicationPolicy(BaseModel):
    """External communication channels policy (WhatsApp, Telegram, Desktop)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    allowed_channels: List[str] = Field(
        default_factory=lambda: ["desktop"],
        description="Permitted channels for interaction.",
    )
    outbound_approval_required: bool = Field(
        default=True,
        description="Whether outbound messages to external channels require human approval.",
    )
    max_outbound_per_minute: int = Field(
        default=10,
        ge=1,
        description="Rate limit on outbound communications.",
    )


class MemoryPolicy(BaseModel):
    """Unified memory access boundaries."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    max_sensitivity_allowed: str = Field(
        default="internal",
        description="Maximum memory sensitivity tier accessible (public, internal, confidential, restricted).",
    )
    allow_cross_contact: bool = Field(
        default=False,
        description="Strictly False: contacts cannot cross privacy boundaries.",
    )


class ApprovalPolicy(BaseModel):
    """Policy-mandated approval gates."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    consequential_actions_require_approval: bool = Field(
        default=True,
        description="Whether consequential actions require human approval.",
    )
    mandatory_approval_actions: List[str] = Field(
        default_factory=lambda: [
            "filesystem.delete",
            "shell.execute",
            "network.egress",
            "credential.access",
        ],
        description="Actions that strictly require user confirmation regardless of autonomy mode.",
    )


class SecurityPolicy(BaseModel):
    """Centralized security policy governing what SAGE is permitted to access."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(default="Standard Security Policy", description="Policy name.")
    filesystem: FilesystemPolicy = Field(default_factory=FilesystemPolicy)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    shell: ShellPolicy = Field(default_factory=ShellPolicy)
    tools: ToolPolicy = Field(default_factory=ToolPolicy)
    communication: CommunicationPolicy = Field(default_factory=CommunicationPolicy)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    restricted_resources: List[str] = Field(
        default_factory=lambda: [
            "C:\\Windows\\System32",
            ".aegis/master.key",
            "vault.enc",
        ],
        description="System resources that cannot be modified under any circumstance.",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ==============================================================================
# DIMENSION 3: EXECUTION PROFILE
# ==============================================================================


@unique
class ExecutionProfileType(str, Enum):
    """Canonical execution profile archetypes."""

    FAST = "FAST"
    BALANCED = "BALANCED"
    DEEP = "DEEP"
    CUSTOM = "CUSTOM"


class ExecutionProfile(BaseModel):
    """Execution profile defining HOW a task is performed."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    profile_type: ExecutionProfileType = Field(
        default=ExecutionProfileType.BALANCED,
        description="Profile archetype category.",
    )
    name: str = Field(default="Standard Balanced", description="Profile display name.")
    model_preference: Optional[str] = Field(
        default=None,
        description="Optional preferred reasoning provider/model.",
    )
    reasoning_intensity: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Intensity of pre-execution planning and reflection (0.0=minimal, 1.0=exhaustive).",
    )
    context_token_budget: int = Field(
        default=4096,
        ge=512,
        le=32768,
        description="Maximum context window token budget for planner.",
    )
    max_total_steps: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum total step execution budget.",
    )
    max_replan_attempts: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum observation-driven replans permitted.",
    )
    verification_depth: str = Field(
        default="standard",
        description="Verification intensity ('minimal', 'standard', 'thorough').",
    )
    timeout_seconds: float = Field(
        default=120.0,
        ge=5.0,
        description="Overall task execution timeout in seconds.",
    )
    retry_policy: Dict[str, Any] = Field(
        default_factory=lambda: {"max_retries": 2, "backoff_ms": 500},
        description="Idempotent step retry configuration.",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def builtin_fast(cls) -> ExecutionProfile:
        return cls(
            profile_type=ExecutionProfileType.FAST,
            name="Fast Execution",
            reasoning_intensity=0.2,
            context_token_budget=2048,
            max_total_steps=10,
            max_replan_attempts=1,
            verification_depth="minimal",
            timeout_seconds=30.0,
        )

    @classmethod
    def builtin_balanced(cls) -> ExecutionProfile:
        return cls(
            profile_type=ExecutionProfileType.BALANCED,
            name="Balanced Execution",
            reasoning_intensity=0.5,
            context_token_budget=4096,
            max_total_steps=20,
            max_replan_attempts=3,
            verification_depth="standard",
            timeout_seconds=120.0,
        )

    @classmethod
    def builtin_deep(cls) -> ExecutionProfile:
        return cls(
            profile_type=ExecutionProfileType.DEEP,
            name="Deep Analysis",
            reasoning_intensity=0.9,
            context_token_budget=8192,
            max_total_steps=40,
            max_replan_attempts=5,
            verification_depth="thorough",
            timeout_seconds=300.0,
        )


# ==============================================================================
# INDEPENDENT DIMENSION: PERSONALITY CONFIGURATION
# ==============================================================================


class PersonalityConfig(BaseModel):
    """Personality configuration independent of authorization and execution."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_name: str = Field(default="Friday", description="Personality display name.")
    tone: str = Field(
        default="helpful, concise, technically rigorous",
        description="Conversational tone style.",
    )
    verbosity: str = Field(
        default="medium",
        description="Output verbosity ('low', 'medium', 'high').",
    )
    presence_state: str = Field(
        default="AVAILABLE",
        description="Availability status ('AVAILABLE', 'BUSY', 'DND', 'OFF').",
    )
    proactive_suggestions: bool = Field(
        default=False,
        description="Whether to suggest optimizations autonomously in conversation.",
    )
    custom_system_prompt: Optional[str] = Field(
        default=None,
        description="Optional custom personality guidance prompt.",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ==============================================================================
# HIERARCHY LAYERS & CUSTOM MODES
# ==============================================================================


@unique
class ConfigurationLayer(str, Enum):
    """Precedence hierarchy for configuration inheritance."""

    SYSTEM_DEFAULTS = "SYSTEM_DEFAULTS"  # Precedence 0 (Base)
    GLOBAL = "GLOBAL"                    # Precedence 1
    USER = "USER"                        # Precedence 2
    AGENT = "AGENT"                      # Precedence 3
    TASK = "TASK"                        # Precedence 4
    AUTOMATION = "AUTOMATION"            # Precedence 5 (Highest task-specific)


class CustomMode(BaseModel):
    """Reusable multi-dimensional configuration mode."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    mode_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(description="Unique human-readable mode name (e.g. 'Coding Mode').")
    description: str = Field(default="", description="Mode purpose and scope.")
    autonomy_profile: AutonomyProfile = Field(default_factory=AutonomyProfile.builtin_assisted)
    security_policy: SecurityPolicy = Field(default_factory=SecurityPolicy)
    execution_profile: ExecutionProfile = Field(default_factory=ExecutionProfile.builtin_balanced)
    personality_config: PersonalityConfig = Field(default_factory=PersonalityConfig)
    is_default: bool = Field(default=False)
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ResolvedConfiguration(BaseModel):
    """Effective resolved configuration with full provenance tracking."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    autonomy_profile: AutonomyProfile
    security_policy: SecurityPolicy
    execution_profile: ExecutionProfile
    personality_config: PersonalityConfig
    active_mode: Optional[str] = None
    effective_layer: ConfigurationLayer = ConfigurationLayer.SYSTEM_DEFAULTS
    field_origins: Dict[str, str] = Field(default_factory=dict)
    resolved_at: str = Field(default_factory=_utc_now_iso)

    def explain(self) -> str:
        """Produce a clean, explainable provenance summary of the active configuration."""
        lines = [
            "=== SAGE RESOLVED CONFIGURATION EXPLANATION ===",
            f"Active Mode:       {self.active_mode or 'Default'}",
            f"Effective Layer:   {self.effective_layer.value}",
            f"Autonomy Mode:     {self.autonomy_profile.mode.value} (Tier {self.autonomy_profile.tier.value})",
            f"Execution Profile: {self.execution_profile.profile_type.value} (Budget: {self.execution_profile.context_token_budget} tok, Timeout: {self.execution_profile.timeout_seconds:.1f}s)",
            f"Personality Name:  {self.personality_config.agent_name} (State: {self.personality_config.presence_state})",
            f"Network Mode:      {self.security_policy.network.mode.value}",
            f"Shell Enabled:     {self.security_policy.shell.enabled}",
            "",
            "Configuration Field Provenance:",
        ]
        for field_path, origin in sorted(self.field_origins.items()):
            lines.append(f" - {field_path:<40} -> {origin}")
        return "\n".join(lines)


# ==============================================================================
# POLICY EVALUATION CONTRACTS
# ==============================================================================


@unique
class PolicyEvaluationDecisionType(str, Enum):
    """Central policy evaluation decisions."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DEFER = "DEFER"


class PolicyEvaluationRequest(BaseModel):
    """Request payload submitted for centralized policy evaluation."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    action_name: str = Field(description="Canonical action name (e.g. 'filesystem.write').")
    tool_name: Optional[str] = Field(default=None, description="Optional tool name invoking action.")
    actor: str = Field(default="supervisor", description="Calling identity.")
    user_id: Optional[str] = Field(default="user", description="Authenticated user context.")
    project_id: Optional[str] = Field(default=None, description="Active project context.")
    target_resource: Optional[str] = Field(default=None, description="Target path or resource.")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Action arguments.")
    active_mode: Optional[str] = Field(default=None, description="Active mode name if set.")


class PolicyEvaluationDecision(BaseModel):
    """Structured decision returned by the Central Policy Evaluation Engine."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    decision: PolicyEvaluationDecisionType
    action_name: str
    reason: str
    rule_references: List[str] = Field(default_factory=list)
    action_risk: str = "LOW"
    autonomy_mode: str = "ASSISTED"
    approval_required: bool = False
    governing_layer: str = "SecurityPolicy"
    timestamp: str = Field(default_factory=_utc_now_iso)

    def is_allowed(self) -> bool:
        return self.decision == PolicyEvaluationDecisionType.ALLOW
