"""SAGE Skills Subsystem -- Public API Surface.

Establishes the deterministic foundation for:
- Declarative Skill Manifests & Validation.
- Deterministic Skill Registry & Lifecycle Management.
- Tool Filtering (ToolScope principle).
- Skill-to-Tool Binding & Trust Core Enforcement.
- Resource & Concurrency Admission.
- Clarification Gate.
- Adaptive Execution & Experience Event Telemetry.
- Checkpoint Integration (AgentRewind principle).

Core Architectural Invariants:
1. Intelligence proposes; deterministic authority decides.
2. Skill cannot bypass Trust Core or Tool Broker.
3. Skill cannot modify Trust Core policy.
4. Experience is evidence, not authority.
5. Adaptive processing is strictly decoupled from the critical execution path.
"""

from src.skills.admission import AdmissionDecision, SkillAdmissionController
from src.skills.checkpoint import SkillCheckpointMetadata, persist_skill_checkpoint
from src.skills.clarification import (
    ClarificationAction,
    ClarificationGate,
    ClarificationReason,
    ClarificationResult,
)
from src.skills.discovery import ToolDiscoveryService
from src.skills.errors import (
    ClarificationError,
    FeedbackError,
    SkillDuplicateError,
    SkillError,
    SkillExecutionError,
    SkillLifecycleError,
    SkillManifestValidationError,
    SkillNotFoundError,
    SkillRegistrationError,
    SkillResourceAdmissionError,
    SkillToolAccessDeniedError,
    SkillToolBindingViolationError,
)
from src.skills.executor import (
    BoundSkillToolBroker,
    DeterministicSkillExecutor,
    SkillExecutor,
)
from src.skills.experience import (
    ExperienceEvent,
    ExperienceFeedbackService,
    ExperienceOutcomeType,
    UserFeedbackType,
)
from src.skills.models import (
    ConcurrencyPolicy,
    ExecutionStrategy,
    MemoryAccessPolicy,
    ProjectScope,
    RegisteredSkill,
    SkillExecutionStatus,
    SkillInvocation,
    SkillLifecycleState,
    SkillManifest,
    SkillRecoveryPolicy,
    SkillResourceRequirement,
    SkillResult,
    SkillType,
    SkillVerificationPolicy,
    VerificationStatus,
)
from src.skills.registry import VALID_LIFECYCLE_TRANSITIONS, SkillRegistry
from src.skills.validation import validate_skill_manifest

__all__ = [
    # Models & Manifests
    "SkillManifest",
    "RegisteredSkill",
    "SkillInvocation",
    "SkillResult",
    "SkillType",
    "SkillLifecycleState",
    "ExecutionStrategy",
    "MemoryAccessPolicy",
    "ProjectScope",
    "SkillVerificationPolicy",
    "SkillRecoveryPolicy",
    "SkillExecutionStatus",
    "VerificationStatus",
    "SkillResourceRequirement",
    "ConcurrencyPolicy",
    # Registry & Validation
    "SkillRegistry",
    "VALID_LIFECYCLE_TRANSITIONS",
    "validate_skill_manifest",
    # Discovery & Filtering
    "ToolDiscoveryService",
    # Execution & Tool Bindings
    "BoundSkillToolBroker",
    "SkillExecutor",
    "DeterministicSkillExecutor",
    # Admission & Concurrency
    "SkillAdmissionController",
    "AdmissionDecision",
    # Clarification
    "ClarificationGate",
    "ClarificationAction",
    "ClarificationReason",
    "ClarificationResult",
    # Experience & Adaptive Execution
    "ExperienceEvent",
    "ExperienceOutcomeType",
    "UserFeedbackType",
    "ExperienceFeedbackService",
    # Checkpoints
    "SkillCheckpointMetadata",
    "persist_skill_checkpoint",
    # Errors
    "SkillError",
    "SkillManifestValidationError",
    "SkillRegistrationError",
    "SkillDuplicateError",
    "SkillNotFoundError",
    "SkillLifecycleError",
    "SkillToolBindingViolationError",
    "SkillToolAccessDeniedError",
    "SkillResourceAdmissionError",
    "SkillExecutionError",
    "ClarificationError",
    "FeedbackError",
]
