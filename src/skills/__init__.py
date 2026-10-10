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
from src.skills.first_party import (
    DEV_VERIFY_MANIFEST,
    RESEARCH_DIAGNOSE_MANIFEST,
    WORKSPACE_INSPECT_MANIFEST,
    dev_verify_handler,
    register_first_party_skills,
    research_diagnose_handler,
    workspace_inspect_handler,
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
    # First-Party Skills
    "WORKSPACE_INSPECT_MANIFEST",
    "DEV_VERIFY_MANIFEST",
    "RESEARCH_DIAGNOSE_MANIFEST",
    "workspace_inspect_handler",
    "dev_verify_handler",
    "research_diagnose_handler",
    "register_first_party_skills",
    # Golden Task Evaluation
    "GoldenTaskRecord",
    "GoldenTaskEvaluator",
    # Admission & Concurrency
    "SkillAdmissionController",
    "AdmissionDecision",
    # Clarification
    "ClarificationGate",
    "ClarificationAction",
    "ClarificationReason",
    "ClarificationResult",
    "ExperienceEvent",
    "ExperienceOutcomeType",
    "UserFeedbackType",
    "ExperienceFeedbackService",
    # NOTE: Lesson types (ExecutionLesson, LessonManager, etc.) live in src.memory.
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


def __getattr__(name: str) -> object:
    if name in ("GoldenTaskEvaluator", "GoldenTaskRecord"):
        from src.skills.evaluation import GoldenTaskEvaluator, GoldenTaskRecord

        if name == "GoldenTaskEvaluator":
            return GoldenTaskEvaluator
        return GoldenTaskRecord
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
