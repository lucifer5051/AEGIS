"""SAGE Error Classification & Research Loop Subsystem.

Canonical Reference:
    PROMPT 029: Persistent Completion Mode Error Investigation & Research
    Principle: CLASSIFY -> INVESTIGATE -> RESEARCH IF NEEDED -> FORM HYPOTHESIS
               -> CHOOSE RECOVERY -> REAUTHORIZE -> RETRY/REPLAN -> VERIFY

Invariants:
- All models are frozen and immutable (extra="forbid").
- Zero-Secret Invariant: Error signatures and research records must never store credentials.
- Research outputs are strictly advisory evidence -- never autonomous execution instructions.
- Research cannot grant authorization or override Trust Core boundaries.
- Offline-by-construction: Providers support local offline documentation & deterministic fixtures.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.manager.models import StepPlan
from src.trust.audit import _SECRET_PATTERNS, _check_no_secrets

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


# ==============================================================================
# ENUMERATIONS
# ==============================================================================


@unique
class ErrorClass(str, Enum):
    """Classification of tool and execution failures."""

    TRANSIENT = "TRANSIENT"  # Timeouts, busy locks, network blip, temporary retryable
    ENVIRONMENT_RESOURCE = "ENVIRONMENT_RESOURCE"  # Memory, disk, missing file/directory
    CONFIGURATION_PARAM = "CONFIGURATION_PARAM"  # Bad type, missing required key, invalid value
    PERMISSION_AUTH = "PERMISSION_AUTH"  # Capability denied, policy restriction, approval needed
    LOGIC_STRATEGY = (
        "LOGIC_STRATEGY"  # Output verification failed, assertion error, incorrect result
    )
    TOOL_INTERFACE = "TOOL_INTERFACE"  # Tool unregistered, bad signature, contract violation
    UNKNOWN = "UNKNOWN"


@unique
class RecoveryStrategyType(str, Enum):
    """Taxonomy of recovery actions available to the persistent loop."""

    RETRY_WITH_BACKOFF = "RETRY_WITH_BACKOFF"
    PARAM_ADJUSTMENT = "PARAM_ADJUSTMENT"
    ALTERNATIVE_TOOL = "ALTERNATIVE_TOOL"
    ESCALATE_APPROVAL = "ESCALATE_APPROVAL"
    REPLAN = "REPLAN"
    ABORT_BLOCKER = "ABORT_BLOCKER"


# ==============================================================================
# DATA MODELS
# ==============================================================================


class ErrorSignature(BaseModel):
    """Normalized fingerprint of an execution failure for deduplication and stuck-loop detection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_name: str
    tool_name: Optional[str] = None
    error_type: str = "ExecutionError"
    normalized_pattern: str = ""

    def key(self) -> str:
        """Produce a canonical string key for fingerprint comparisons."""
        tool = self.tool_name or "none"
        return f"{tool}:{self.action_name}:{self.error_type}:{self.normalized_pattern}"


class ResearchSource(BaseModel):
    """Authoritative reference source cited by research."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(description="Title of documentation or issue reference.")
    url: str = Field(description="Normalized reference URL or local doc path.")
    retrieval_time: str = Field(description="ISO-8601 UTC timestamp when retrieved.")
    excerpt: str = Field(description="Relevant bounded excerpt from the source.")
    domain: str = Field(default="", description="Source domain name (e.g. docs.python.org).")
    is_official: bool = Field(default=True, description="Whether source is authoritative/official.")


class OnlineResearchBudget(BaseModel):
    """Bounded limits on online documentation research."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Primary fields
    max_queries_per_step: int = Field(default=2, ge=1, le=10)
    # max_queries_per_diagnosis is an alias for max_queries_per_step
    max_queries_per_diagnosis: int = Field(default=2, ge=1, le=10)
    max_sources_per_query: int = Field(default=5, ge=1, le=20)
    max_total_queries_per_task: int = Field(default=5, ge=1, le=50)
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    max_response_bytes: int = Field(default=100_000, ge=1024, le=1_000_000)
    # max_bytes_per_source applies per-source excerpt truncation
    max_bytes_per_source: int = Field(default=500, ge=64, le=10_000)
    max_retries: int = Field(default=2, ge=0, le=5)
    allowed_domains: List[str] = Field(
        default_factory=lambda: [
            "docs.python.org",
            "pypi.org",
            "github.com",
            "readthedocs.io",
            "stackoverflow.com",
            "learn.microsoft.com",
            "sqlite.org",
            "pydantic.dev",
            "pytest.org",
        ]
    )


class ResearchRecord(BaseModel):
    """Documented evidence from inspecting local or authoritative reference sources."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    research_id: str = Field(description="Unique research query record UUID.")
    query: str = Field(description="Normalized research query text.")
    error_signature: Optional[str] = Field(
        default=None, description="Error signature query was formulated for."
    )
    source_references: List[str] = Field(
        default_factory=list, description="Authoritative reference URLs or local doc paths."
    )
    findings_summary: str = Field(description="Extracted empirical finding or recommendation.")
    suggested_adjustments: Dict[str, Any] = Field(
        default_factory=dict, description="Recommended parameter or strategy adjustments."
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Epistemic confidence.")
    sources: List[ResearchSource] = Field(
        default_factory=list, description="Structured source citations."
    )
    provider_type: str = Field(
        default="LOCAL", description="Provider origin ('LOCAL', 'ONLINE', or 'COMPOSITE')."
    )
    relevance_explanation: Optional[str] = Field(
        default=None, description="Explanation of relevance to the diagnosed failure."
    )
    proposed_explanation: Optional[str] = Field(
        default=None, description="Proposed root cause explanation."
    )
    verification_method: Optional[str] = Field(
        default=None, description="Method to empirically verify or disprove the finding."
    )
    created_at: str = Field(description="ISO-8601 UTC timestamp.")


class ErrorDiagnosis(BaseModel):
    """Complete diagnostic hypothesis and recovery prescription for a step failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnosis_id: str = Field(description="Unique diagnosis UUID.")
    step_id: str = Field(description="Failing step UUID.")
    step_index: int = Field(description="Step index in task.")
    error_signature: str = Field(description="Normalized error key.")
    error_class: ErrorClass = Field(description="Classified failure category.")
    suspected_cause: str = Field(description="Hypothesis explaining root cause.")
    research_record: Optional[ResearchRecord] = Field(
        default=None, description="Supporting research evidence, if conducted."
    )
    chosen_strategy: RecoveryStrategyType = Field(description="Prescribed recovery strategy.")
    proposed_step: Optional[StepPlan] = Field(
        default=None, description="Replacement or recovery step plan."
    )
    recovery_arguments: Dict[str, Any] = Field(
        default_factory=dict, description="Adjusted arguments for retry."
    )
    requires_user_reconfirmation: bool = Field(
        default=False, description="True if recovery materially changes task scope."
    )
    reconfirmation_reason: Optional[str] = Field(
        default=None, description="Explanation why user reconfirmation is required."
    )
    testable_hypothesis: Optional[str] = Field(
        default=None, description="Empirically testable recovery hypothesis."
    )
    verification_method: Optional[str] = Field(
        default=None, description="Method used to verify hypothesis after recovery step."
    )
    disproven_hypotheses: List[str] = Field(
        default_factory=list, description="Previously disproven hypotheses for this step."
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Diagnostic confidence.")
    created_at: str = Field(description="ISO-8601 UTC timestamp.")


# ==============================================================================
# RESEARCH PROVIDER INTERFACE & OFFLINE IMPLEMENTATION
# ==============================================================================


class ResearchProvider(ABC):
    """Pluggable provider for looking up reference documentation and recovery guidance."""

    @abstractmethod
    def research(
        self,
        query: str,
        error_class: ErrorClass,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ResearchRecord]:
        """Perform lookup against reference documentation and return structured evidence."""


class LocalDocResearchProvider(ResearchProvider):
    """Deterministic, 100% offline research provider backed by an in-memory knowledge table."""

    def __init__(self, knowledge_base: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self._kb = knowledge_base or self._build_default_kb()

    def _build_default_kb(self) -> Dict[str, Dict[str, Any]]:
        return {
            "timeout": {
                "source": "docs/specifications/AEGIS_PERFORMANCE_METRICS_SPECIFICATION.md § 4",
                "finding": "Step timed out; recommend RETRY_WITH_BACKOFF with elevated timeout.",
                "adjustments": {"timeout_seconds": 60},
                "confidence": 0.90,
            },
            "file_not_found": {
                "source": "docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12",
                "finding": "Target directory or file missing; check path or create directory.",
                "adjustments": {"create_parents": True},
                "confidence": 0.95,
            },
            "policy_denied": {
                "source": "docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 2",
                "finding": "Action denied by Trust Core autonomy tier; requires user approval.",
                "adjustments": {"requires_approval": True},
                "confidence": 1.0,
            },
            "type_error": {
                "source": "docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 8",
                "finding": "Tool parameter type mismatch; coerce argument to expected type.",
                "adjustments": {},
                "confidence": 0.85,
            },
        }

    def register_entry(
        self,
        keyword: str,
        source: str,
        finding: str,
        adjustments: Optional[Dict[str, Any]] = None,
        confidence: float = 1.0,
    ) -> None:
        """Register a deterministic test or domain reference entry."""
        self._kb[keyword.lower()] = {
            "source": source,
            "finding": finding,
            "adjustments": adjustments or {},
            "confidence": confidence,
        }

    def research(
        self,
        query: str,
        error_class: ErrorClass,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ResearchRecord]:
        """Search local offline reference knowledge base for relevant recovery guidance."""
        _check_no_secrets(query)
        q_lower = query.lower()
        now = _now_utc()

        for key, entry in self._kb.items():
            if key in q_lower:
                source_ref = entry["source"]
                finding = entry["finding"]
                src = ResearchSource(
                    title=f"Local Architectural Reference: {key}",
                    url=source_ref,
                    retrieval_time=now,
                    excerpt=finding,
                    domain="local",
                    is_official=True,
                )
                error_sig = context.get("error_signature") if context else None
                return ResearchRecord(
                    research_id=str(uuid4()),
                    query=query,
                    error_signature=error_sig,
                    source_references=[source_ref],
                    findings_summary=finding,
                    suggested_adjustments=entry.get("adjustments", {}),
                    confidence=entry.get("confidence", 1.0),
                    sources=[src],
                    provider_type="LOCAL",
                    relevance_explanation=f"Matched local knowledge keyword '{key}'",
                    proposed_explanation=finding,
                    verification_method="Execute step and check output against assertions.",
                    created_at=now,
                )

        return None


# ==============================================================================
# ONLINE RESEARCH SANITIZATION & PROVIDERS
# ==============================================================================

_INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions"),
    re.compile(r"(?i)disregard\s+(?:all\s+)?(?:previous|prior|system|the)\s+"),
    re.compile(r"(?i)system\s+prompt\s*[:\s]"),
    re.compile(r"(?i)you\s+(?:are\s+now|must\s+(?:now\s+)?(?:run|execute|install|download))"),
    re.compile(r"(?i)you\s+are\s+(?:now\s+)?(?:an?\s+)?(?:admin|administrator|root|superuser)"),
    re.compile(r"(?i)curl\s+.*?\|\s*(?:bash|sh)"),
    re.compile(r"(?i)rm\s+-rf"),
    re.compile(r"(?i)sudo\s+"),
    re.compile(r"(?i)chmod\s+\+x"),
    re.compile(r"(?i)powershell\s+-enc"),
    re.compile(r"(?i)act\s+as\s+(?:an?\s+)?(?:admin|assistant|bot|agent)\s+that"),
    re.compile(r"(?i)grant\s+(?:admin|root|full)\s+(?:access|permissions?)"),
]


def _extract_domain(url: str) -> str:
    """Extract lowercase netloc/domain from a URL."""
    try:
        parsed = urlparse(url)
        return parsed.netloc.lower() or parsed.path.lower()
    except Exception:
        return ""


def _sanitize_untrusted_content(text: str, max_chars: int = 500) -> str:
    """Sanitize raw text from external web pages, removing injection attempts and markup."""
    clean = re.sub(r"<[^>]+>", " ", text)
    for pat in _INJECTION_PATTERNS:
        clean = pat.sub("[UNTRUSTED_INSTRUCTION_REDACTED]", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:max_chars].strip()


def _sanitize_query(query: str, max_chars: int = 120) -> str:
    """Sanitize research query text: check secrets, strip local paths and usernames."""
    _check_no_secrets(query)
    clean = re.sub(r"[A-Za-z]:\\[\w\\.-]+", "<path>", query)
    clean = re.sub(r"/(?:Users|home|root|var|etc)/[\w/.-]+", "<path>", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:max_chars].strip()


# Patterns that indicate dangerous content in string adjustments
_DANGEROUS_STRING_PATTERNS = re.compile(
    r"(__import__|eval\s*\(|exec\s*\(|subprocess|os\.system|shutil|open\s*\("
    r"|shell\s*=\s*True|importlib|compile\s*\(|globals\s*\(|locals\s*\()",
    re.IGNORECASE,
)


def _validate_suggested_adjustments(adjustments: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure suggested parameter adjustments from research are safe primitives.

    Rejects adjustments containing shell syntax, malicious code, or disallowed types.
    Only int, float, bool, and safe short strings are permitted.
    """
    safe: Dict[str, Any] = {}
    for k, v in adjustments.items():
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", str(k)):
            continue
        if isinstance(v, bool):
            safe[k] = v
        elif isinstance(v, (int, float)):
            safe[k] = v
        elif isinstance(v, str):
            # Reject strings with shell metacharacters, code injection, or dangerous imports
            if any(c in v for c in (";", "&", "|", "`", "$", "\n", "\r")):
                continue
            if _DANGEROUS_STRING_PATTERNS.search(v):
                continue
            safe[k] = v
    return safe


class OnlineDocResearchProvider(ResearchProvider):
    """Authorized online technical documentation & reference lookup provider.

    Adheres strictly to the following security & resilience invariants:
    1. Network-gated: Returns None unless explicitly authorized via `network_permitted=True`.
    2. Zero-secret: Scrubbed queries; secret leak check before dispatch.
    3. Domain-bounded: Restricts queries and results to authoritative technical domains.
    4. Untrusted data sanitization: Strips markup, neutralizes prompt injection payloads.
    5. Bounded budgets: Limits total queries, sources per query, timeouts, and byte limits.
    6. Non-blocking fallback: Never raises on network down/timeout; returns None for local fallback.
    """

    def __init__(
        self,
        network_permitted: bool = False,
        budget: Optional[OnlineResearchBudget] = None,
        search_backend: Optional[Callable[[str, int, float], List[Dict[str, Any]]]] = None,
    ) -> None:
        self._network_permitted = network_permitted
        self._budget = budget or OnlineResearchBudget()
        self._search_backend = search_backend
        self._query_count = 0

    @property
    def network_permitted(self) -> bool:
        """Whether network research is permitted."""
        return self._network_permitted

    @property
    def query_count(self) -> int:
        """Number of queries dispatched so far."""
        return self._query_count

    @property
    def budget(self) -> OnlineResearchBudget:
        """Configured research budget."""
        return self._budget

    def research(
        self,
        query: str,
        error_class: ErrorClass,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ResearchRecord]:
        """Perform bounded online documentation lookup if network is permitted."""
        if not self._network_permitted:
            return None

        # Use the tighter of max_queries_per_diagnosis or max_total_queries_per_task
        effective_limit = min(
            self._budget.max_queries_per_diagnosis,
            self._budget.max_total_queries_per_task,
        )
        if self._query_count >= effective_limit:
            logger.warning("Online research budget exhausted (%d queries).", self._query_count)
            return None

        # Sanitize query and verify zero secrets
        sanitized_query = _sanitize_query(query)
        self._query_count += 1
        now = _now_utc()

        try:
            results: List[Dict[str, Any]] = []
            if self._search_backend is not None:
                results = self._search_backend(
                    sanitized_query,
                    self._budget.max_sources_per_query,
                    self._budget.timeout_seconds,
                )
            else:
                # Default offline-safe fallback if no backend wired
                results = []

            if not results:
                return None

            sources: List[ResearchSource] = []
            combined_excerpts: List[str] = []
            suggested_adjustments: Dict[str, Any] = {}

            for r in results:
                raw_url = str(r.get("url", ""))
                domain = _extract_domain(raw_url)
                if self._budget.allowed_domains and not any(
                    domain.endswith(d) or d in domain for d in self._budget.allowed_domains
                ):
                    continue

                raw_title = str(r.get("title", "Authoritative Technical Reference"))
                raw_snippet = str(r.get("snippet", r.get("excerpt", "")))
                clean_snippet = _sanitize_untrusted_content(
                    raw_snippet, max_chars=self._budget.max_bytes_per_source
                )

                source = ResearchSource(
                    title=raw_title[:100],
                    url=raw_url[:200],
                    retrieval_time=now,
                    excerpt=clean_snippet,
                    domain=domain,
                    is_official=True,
                )
                sources.append(source)
                combined_excerpts.append(clean_snippet)

                raw_adj = r.get("suggested_adjustments", {})
                if isinstance(raw_adj, dict):
                    valid_adj = _validate_suggested_adjustments(raw_adj)
                    suggested_adjustments.update(valid_adj)

                # Auto-extract common recovery hints from snippet text
                # e.g. "Setting timeout=20 resolved the issue" -> {"timeout": 20}
                timeout_match = re.search(
                    r"(?:timeout|timeout_seconds?|connect_timeout)\s*[=:]\s*(\d+)",
                    clean_snippet,
                    re.IGNORECASE,
                )
                if timeout_match and "timeout" not in suggested_adjustments:
                    try:
                        suggested_adjustments["timeout"] = int(timeout_match.group(1))
                    except ValueError:
                        pass

            if not sources:
                return None

            findings_summary = (
                f"Online documentation findings for '{sanitized_query}': "
                + " ".join(combined_excerpts[:2])
            )[:300]

            error_sig = context.get("error_signature") if context else None
            return ResearchRecord(
                research_id=str(uuid4()),
                query=sanitized_query,
                error_signature=error_sig,
                source_references=[s.url for s in sources],
                findings_summary=findings_summary,
                suggested_adjustments=suggested_adjustments,
                confidence=0.85,
                sources=sources,
                provider_type="ONLINE",
                relevance_explanation=f"Matches classified error {error_class.value}",
                proposed_explanation=(f"Official documentation suggests {findings_summary[:120]}"),
                verification_method=(
                    "Execute recovery step and verify acceptance criteria assertions."
                ),
                created_at=now,
            )

        except (TimeoutError, ConnectionError, OSError) as exc:
            logger.warning("Online research failed gracefully (network/timeout error): %s", exc)
            return None
        except Exception as exc:
            logger.warning("Online research failed unexpectedly: %s", exc)
            return None


class CompositeResearchProvider(ResearchProvider):
    """Hierarchical research provider: Local project documentation -> Online research.

    Enforces the escalation hierarchy:
    1. Search local documentation and approved lessons first.
    2. If no local match and network is permitted, search online documentation.
    3. If online search fails or is disabled, fallback cleanly.
    """

    def __init__(
        self,
        local_provider: Optional[ResearchProvider] = None,
        online_provider: Optional[ResearchProvider] = None,
        network_permitted: bool = False,
    ) -> None:
        self._local: ResearchProvider = local_provider or LocalDocResearchProvider()
        self._online: Optional[ResearchProvider] = online_provider
        self._network_permitted = network_permitted

    @property
    def local_provider(self) -> ResearchProvider:
        """The primary local documentation provider."""
        return self._local

    @property
    def online_provider(self) -> Optional[ResearchProvider]:
        """The secondary online documentation provider."""
        return self._online

    def research(
        self,
        query: str,
        error_class: ErrorClass,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ResearchRecord]:
        """Perform tiered research: local first, then online if unaddressed."""
        # 1. Local research
        local_rec = self._local.research(query=query, error_class=error_class, context=context)
        if local_rec is not None:
            return local_rec

        # 2. Escalate to online research if configured and permitted
        if self._online is not None:
            return self._online.research(query=query, error_class=error_class, context=context)

        return None


# ==============================================================================
# ERROR CLASSIFIER & INVESTIGATOR
# ==============================================================================


class ErrorClassifier:
    """Classifies execution errors into standard taxonomy based on deterministic heuristics."""

    _PATTERNS = [
        (
            ErrorClass.PERMISSION_AUTH,
            re.compile(
                r"(permission|denied|unauthorized|forbidden|trust core|"
                r"capability|approval required)",
                re.IGNORECASE,
            ),
        ),
        (
            ErrorClass.TRANSIENT,
            re.compile(
                r"(timeout|timed out|busy|lock|temporarily unavailable|connection reset)",
                re.IGNORECASE,
            ),
        ),
        (
            ErrorClass.ENVIRONMENT_RESOURCE,
            re.compile(
                r"(no such file|not found|disk full|out of memory|vram|insufficient space)",
                re.IGNORECASE,
            ),
        ),
        (
            ErrorClass.CONFIGURATION_PARAM,
            re.compile(
                r"(valueerror|typeerror|invalid argument|missing\s*required|"
                r"missing.*param|required.*missing|coercion|unknown parameter)",
                re.IGNORECASE,
            ),
        ),
        (
            ErrorClass.TOOL_INTERFACE,
            re.compile(
                r"(tool not registered|unregistered tool|unknown tool|schema mismatch)",
                re.IGNORECASE,
            ),
        ),
        (
            ErrorClass.LOGIC_STRATEGY,
            re.compile(
                r"(assertion|verification failed|unexpected output|test failed)",
                re.IGNORECASE,
            ),
        ),
    ]

    @classmethod
    def classify(cls, error_text: str, error_type: Optional[str] = None) -> ErrorClass:
        """Classify error message into ErrorClass taxonomy."""
        combined = f"{error_type or ''} {error_text}".strip()
        for error_class, pattern in cls._PATTERNS:
            if pattern.search(combined):
                return error_class
        return ErrorClass.UNKNOWN

    @classmethod
    def extract_signature(
        cls,
        action_name: str,
        tool_name: Optional[str],
        error_type: Optional[str],
        error_summary: str,
    ) -> ErrorSignature:
        """Extract a normalized error signature for deduplication and stuck-loop detection."""
        # Strip secrets first to uphold zero-secret invariant
        normalized = error_summary
        for pat in _SECRET_PATTERNS:
            normalized = pat.sub("<secret>", normalized)
        normalized = re.sub(r"\b(?:sk|pk)[_-][A-Za-z0-9_-]+\b", "<secret>", normalized)
        normalized = re.sub(r"(?i)bearer\s+\S+", "Bearer <secret>", normalized)

        # Strip numbers, paths, and UUIDs to normalize the error pattern
        normalized = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", normalized
        )
        normalized = re.sub(r"[A-Za-z]:\\[\w\\.-]+", "<path>", normalized)
        normalized = re.sub(r"/[\w/.-]+", "<path>", normalized)
        normalized = re.sub(r"\d+", "<n>", normalized)
        norm_clean = normalized[:60].strip()

        return ErrorSignature(
            action_name=action_name,
            tool_name=tool_name,
            error_type=error_type or "ExecutionError",
            normalized_pattern=norm_clean,
        )


class DiagnosticInvestigator:
    """Investigates failing steps, local lessons, and research documentation to diagnose."""

    def __init__(
        self,
        research_provider: Optional[ResearchProvider] = None,
        lesson_manager: Optional[Any] = None,
        online_provider: Optional[ResearchProvider] = None,
        network_permitted: bool = False,
        experience_service: Optional[Any] = None,
    ) -> None:
        if research_provider is not None:
            self._research = research_provider
        else:
            local = LocalDocResearchProvider()
            online = online_provider or (
                OnlineDocResearchProvider(network_permitted=network_permitted)
                if network_permitted
                else None
            )
            self._research = CompositeResearchProvider(local_provider=local, online_provider=online)
        self._lessons = lesson_manager or experience_service

    def diagnose(
        self,
        step_id: str,
        step_index: int,
        step_plan: StepPlan,
        error_type: Optional[str],
        error_summary: str,
        consecutive_failures: int = 1,
        previous_strategies: Optional[List[RecoveryStrategyType]] = None,
        disproven_hypotheses: Optional[List[str]] = None,
    ) -> ErrorDiagnosis:
        """Analyze a failure and synthesize a structured diagnosis and recovery strategy.

        Escalation hierarchy:
        1. Inspect error output, logs, environment, and signatures.
        2. Consult approved lessons in LessonManager and local architectural documentation.
        3. If cause remains unfamiliar or recurring and network research is permitted,
           perform bounded online research.
        4. Extract source-backed findings and formulate a testable hypothesis.
        5. Select safe recovery action within Trust Core boundaries.
        6. If previous hypothesis was disproven, switch to an alternate strategy.
        """
        _check_no_secrets(error_summary)
        now = _now_utc()

        err_class = ErrorClassifier.classify(error_summary, error_type)
        sig = ErrorClassifier.extract_signature(
            action_name=step_plan.action_name,
            tool_name=step_plan.tool_name,
            error_type=error_type,
            error_summary=error_summary,
        )

        # 1. Check applicable approved lessons
        relevant_lesson = None
        if self._lessons is not None:
            try:
                approved_lessons = self._lessons.get_approved_lessons()
                for les in approved_lessons:
                    if (
                        les.trigger_condition.lower() in error_summary.lower()
                        or step_plan.action_name.lower() in les.trigger_condition.lower()
                    ):
                        relevant_lesson = les
                        break
            except Exception as exc:
                logger.warning("Lesson lookup during diagnosis failed: %s", exc)

        # 2. Check research provider if error is unfamiliar or recurring
        research_rec: Optional[ResearchRecord] = None
        # Transient errors: allow research on repeated failures to find more targeted fix
        should_research = self._research is not None and (
            err_class != ErrorClass.TRANSIENT or consecutive_failures > 1
        )
        if should_research:
            research_query = f"{step_plan.action_name} {err_class.value} {error_summary[:80]}"
            research_rec = self._research.research(
                query=research_query,
                error_class=err_class,
                context={
                    "action_name": step_plan.action_name,
                    "tool_name": step_plan.tool_name,
                    "error_signature": sig.key(),
                    "consecutive_failures": consecutive_failures,
                },
            )

        # 3. Determine recovery strategy and formulate testable hypothesis
        suspected_cause = f"Failure categorized as {err_class.value}: {error_summary}"
        chosen_strategy = RecoveryStrategyType.RETRY_WITH_BACKOFF
        recovery_args = dict(step_plan.arguments)
        proposed_step: Optional[StepPlan] = None
        requires_reconfirmation = False
        reconfirmation_reason: Optional[str] = None
        testable_hypothesis: Optional[str] = None
        verification_method: Optional[str] = None

        if err_class == ErrorClass.PERMISSION_AUTH:
            chosen_strategy = RecoveryStrategyType.ESCALATE_APPROVAL
            requires_reconfirmation = True
            suspected_cause = f"Trust Core policy denied '{step_plan.action_name}': {error_summary}"
            reconfirmation_reason = (
                f"Action '{step_plan.action_name}' was denied by Trust Core policy; "
                "user authorization approval is required."
            )
            testable_hypothesis = (
                "Action blocked by Trust Core policy; requires approval to proceed."
            )
            verification_method = "Awaiting user approval before continuing."
        elif err_class == ErrorClass.CONFIGURATION_PARAM:
            chosen_strategy = RecoveryStrategyType.PARAM_ADJUSTMENT
            if research_rec and research_rec.suggested_adjustments:
                recovery_args.update(research_rec.suggested_adjustments)
                suspected_cause = (
                    f"Parameter invalid; adjusted per {research_rec.provider_type} research: "
                    f"{research_rec.findings_summary[:120]}"
                )
                testable_hypothesis = (
                    research_rec.proposed_explanation
                    or "Adjusted parameter values satisfy schema requirements."
                )
                verification_method = (
                    research_rec.verification_method
                    or "Execute step with adjusted parameters and verify completed status."
                )
            else:
                suspected_cause = (
                    "Tool parameter invalid or missing; adjusted based on schema/docs."
                )
                testable_hypothesis = "Standard parameter adjustment resolves configuration error."
                verification_method = "Execute step and check completed status."
        elif err_class == ErrorClass.TRANSIENT:
            chosen_strategy = RecoveryStrategyType.RETRY_WITH_BACKOFF
            testable_hypothesis = "Failure was transient; subsequent retry will succeed."
            verification_method = "Retry step with exponential backoff."
            if research_rec is not None and research_rec.suggested_adjustments:
                # Research found targeted adjustments — prefer parameter fix over blind retry
                chosen_strategy = RecoveryStrategyType.PARAM_ADJUSTMENT
                recovery_args.update(research_rec.suggested_adjustments)
                suspected_cause = (
                    f"Transient failure guided by {research_rec.provider_type} research: "
                    f"{research_rec.findings_summary[:120]}"
                )
                testable_hypothesis = (
                    research_rec.proposed_explanation
                    or f"Applying research-suggested adjustments resolves {err_class.value}."
                )
                verification_method = (
                    research_rec.verification_method
                    or "Execute step with adjusted parameters and verify completed status."
                )
            elif consecutive_failures > 2:
                # If transient error persists 3 times, switch strategy
                chosen_strategy = RecoveryStrategyType.REPLAN
                suspected_cause = (
                    "Transient failure did not resolve after multiple retries; replanning."
                )
                testable_hypothesis = "Replanned sequence bypasses persistent transient blocker."
                verification_method = "Formulate new plan and verify subsequent steps."
        elif err_class in (
            ErrorClass.LOGIC_STRATEGY,
            ErrorClass.ENVIRONMENT_RESOURCE,
            ErrorClass.TOOL_INTERFACE,
            ErrorClass.UNKNOWN,
        ):
            if relevant_lesson is not None:
                suspected_cause = (
                    f"Guided by approved lesson [{relevant_lesson.lesson_id[:8]}]: "
                    f"{relevant_lesson.inferred_explanation}"
                )
                chosen_strategy = RecoveryStrategyType.PARAM_ADJUSTMENT
                testable_hypothesis = (
                    f"Approved lesson hypothesis: {relevant_lesson.inferred_explanation}"
                )
                verification_method = (
                    "Execute step with lesson recommendation and verify StepVerifier outcome."
                )
            elif research_rec is not None:
                chosen_strategy = RecoveryStrategyType.PARAM_ADJUSTMENT
                if research_rec.suggested_adjustments:
                    recovery_args.update(research_rec.suggested_adjustments)
                suspected_cause = (
                    f"Guided by {research_rec.provider_type} research: "
                    f"{research_rec.findings_summary[:120]}"
                )
                testable_hypothesis = (
                    research_rec.proposed_explanation
                    or f"Research recovery hypothesis: {research_rec.findings_summary[:80]}"
                )
                verification_method = (
                    research_rec.verification_method
                    or "Execute replacement step and verify StepVerifier assertions."
                )
            elif consecutive_failures > 1:
                chosen_strategy = RecoveryStrategyType.REPLAN
                requires_reconfirmation = True
                reconfirmation_reason = (
                    f"Step '{step_plan.action_name}' failed verification repeatedly; "
                    "replanning with alternative strategy requires confirmation."
                )
                testable_hypothesis = "Replanning step sequence will bypass failing condition."
                verification_method = "Execute replacement step in new plan."
            else:
                chosen_strategy = RecoveryStrategyType.PARAM_ADJUSTMENT
                testable_hypothesis = (
                    f"Standard recovery for {err_class.value}: retry with adjusted arguments."
                )
                verification_method = "Execute step and check completed status."

        # 4. Check against previously disproven hypotheses
        disproven = list(disproven_hypotheses or [])
        if len(disproven) >= 2:
            chosen_strategy = RecoveryStrategyType.REPLAN
            suspected_cause = (
                f"Multiple recovery hypotheses were disproven ({len(disproven)} attempts); "
                "replanning workflow sequence."
            )
            testable_hypothesis = "Replanned sequence circumvents disproven failure modes."
            verification_method = "Validate replanned sequence against verifier."
            requires_reconfirmation = True
            reconfirmation_reason = (
                "Multiple recovery hypotheses were empirically disproven; "
                "user reconfirmation required for replanning."
            )
        elif testable_hypothesis and testable_hypothesis in disproven:
            # The hypothesis was tested and failed on a previous attempt!
            # Must switch to an alternate strategy rather than repeating the same approach.
            if chosen_strategy == RecoveryStrategyType.PARAM_ADJUSTMENT:
                chosen_strategy = RecoveryStrategyType.ALTERNATIVE_TOOL
                suspected_cause = (
                    f"Previous hypothesis '{testable_hypothesis}' was disproven; "
                    "switching to alternative tool strategy."
                )
                testable_hypothesis = (
                    "Alternative tool strategy bypassing disproven hypothesis "
                    f"({len(disproven)} failed)."
                )
                verification_method = "Execute alternative tool and verify acceptance criteria."
                requires_reconfirmation = True
                reconfirmation_reason = (
                    "Previous hypothesis was disproven; "
                    "switching to alternative tool strategy requires user confirmation."
                )
            elif chosen_strategy == RecoveryStrategyType.ALTERNATIVE_TOOL:
                chosen_strategy = RecoveryStrategyType.REPLAN
                suspected_cause = (
                    "Previous recovery hypotheses were disproven; replanning workflow sequence."
                )
                testable_hypothesis = "Replanned sequence circumvents disproven failure modes."
                verification_method = "Validate replanned sequence against verifier."
                requires_reconfirmation = True
                reconfirmation_reason = (
                    "Multiple recovery hypotheses were empirically disproven; "
                    "user reconfirmation required for replanning."
                )

        # 5. Oscillation check: if strategies are cycling or repeating after multiple attempts
        prev = previous_strategies or []
        if len(prev) >= 2 and (
            chosen_strategy in prev or (prev[-1] != chosen_strategy and prev[-2] == chosen_strategy)
        ):
            chosen_strategy = RecoveryStrategyType.ABORT_BLOCKER
            suspected_cause = (
                "Oscillating recovery strategies detected with no measurable progress."
            )
            requires_reconfirmation = True
            reconfirmation_reason = "Stuck loop detected: oscillating recovery strategies."

        return ErrorDiagnosis(
            diagnosis_id=str(uuid4()),
            step_id=step_id,
            step_index=step_index,
            error_signature=sig.key(),
            error_class=err_class,
            suspected_cause=suspected_cause,
            research_record=research_rec,
            chosen_strategy=chosen_strategy,
            proposed_step=proposed_step,
            recovery_arguments=recovery_args,
            requires_user_reconfirmation=requires_reconfirmation,
            reconfirmation_reason=reconfirmation_reason,
            testable_hypothesis=testable_hypothesis,
            verification_method=verification_method,
            disproven_hypotheses=disproven,
            confidence=0.90 if research_rec else 0.80,
            created_at=now,
        )
