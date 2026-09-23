"""AEGIS Fast Decision Engine Package.

Document Reference: docs/specifications/AEGIS_FAST_DECISION_ENGINE.md
"""

from src.decision.datasets import (
    ADVERSARIAL_EDGE_TEST_SET,
    GOLDEN_ROUTING_QUALIFICATION_SET,
)
from src.decision.errors import (
    AdmissionBusyError,
    ChecksumMismatchError,
    FastDecisionError,
    HardwareConstraintError,
    InferenceTimeoutError,
    LowConfidenceError,
    MissingModelFileError,
    ModelIntegrityError,
    ProviderUnavailableError,
    SecurityViolationError,
    TokenBudgetExceededError,
)
from src.decision.models import (
    AEGIS_FAST_PATH_MAX_BUDGET,
    AlternativeRoute,
    DecisionOutcome,
    DecisionRoute,
    FallbackReason,
    FastDecisionRequest,
    FastDecisionResult,
    ProviderMetrics,
    ProviderStatus,
    RequestContext,
)
from src.decision.providers import (
    VON_AUDITED_COMMIT,
    FastDecisionProvider,
    VonFastDecisionProvider,
)
from src.decision.service import (
    CANONICAL_INTENT_CRITERIA,
    DEFAULT_QUALIFIED_THRESHOLDS,
    FastDecisionService,
)

__all__ = [
    # Models & Constants
    "AEGIS_FAST_PATH_MAX_BUDGET",
    "DecisionRoute",
    "ProviderStatus",
    "DecisionOutcome",
    "FallbackReason",
    "RequestContext",
    "FastDecisionRequest",
    "AlternativeRoute",
    "FastDecisionResult",
    "ProviderMetrics",
    # Service & Criteria
    "FastDecisionService",
    "CANONICAL_INTENT_CRITERIA",
    "DEFAULT_QUALIFIED_THRESHOLDS",
    # Providers
    "FastDecisionProvider",
    "VonFastDecisionProvider",
    "VON_AUDITED_COMMIT",
    # Errors
    "FastDecisionError",
    "ProviderUnavailableError",
    "ModelIntegrityError",
    "MissingModelFileError",
    "ChecksumMismatchError",
    "HardwareConstraintError",
    "TokenBudgetExceededError",
    "AdmissionBusyError",
    "InferenceTimeoutError",
    "LowConfidenceError",
    "SecurityViolationError",
    # Datasets
    "GOLDEN_ROUTING_QUALIFICATION_SET",
    "ADVERSARIAL_EDGE_TEST_SET",
]
