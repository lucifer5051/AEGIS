"""SAGE EKM Memory Models -- Typed Schemas for Experience & Knowledge Memory.

Canonical Reference:
- docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
- docs/specifications/AEGIS_DOMAIN_SCHEMA_FOUNDATION.md

Invariants:
- Canonical trust levels frozen at M0-M4.
- Immutable Pydantic models with extra='forbid'.
- Global scope represented by project_id IS NULL.
- Historical records preserved via supersession chain.
- No raw secrets permitted in content, tags, or metadata.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.storage.trust_levels import MemoryTrustLevel


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryCategory(str, Enum):
    """Supported typed categories for EKM memory records."""

    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    EXPERIENCE = "experience"
    LESSON = "lesson"
    PROCEDURE = "procedure"
    ENTITY = "entity"
    RELATION = "relation"
    EVIDENCE = "evidence"
    SESSION = "session"
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"
    PERSONAL = "personal"
    WORKING = "working"
    RELATIONAL = "relational"
    KNOWLEDGE = "knowledge"
    CONCEPT = "concept"
    INTERACTION = "interaction"
    WORKFLOW = "workflow"
    USER_PROFILE = "user_profile"
    SCRATCHPAD = "scratchpad"
    ENTITY_RELATION = "entity_relation"


@unique
class CognitiveMemoryCategory(str, Enum):
    """The 6 fundamental cognitive memory categories in SAGE Unified Memory."""

    SEMANTIC = "semantic"       # Facts, knowledge, concepts, technical domain information
    EPISODIC = "episodic"       # Conversations, events, decisions, experiences, task history
    PROCEDURAL = "procedural"   # Workflows, procedures, reusable instructions, lessons
    PERSONAL = "personal"       # User preferences, goals, relationships, personal context
    WORKING = "working"         # Active task/conversation temporary context
    RELATIONAL = "relational"   # Links, connections between entities/people/tasks/memories


@unique
class MemorySensitivity(str, Enum):
    """Security classification sensitivity tiers for memory access."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


@unique
class MemoryProvenanceType(str, Enum):
    """Epistemic provenance origin for a memory record."""

    EXPLICIT_USER = "explicit_user"
    EXTRACTED = "extracted"
    INFERRED = "inferred"
    EXTERNAL = "external"
    EXECUTION = "execution"


def map_to_cognitive_category(category: MemoryCategory | str) -> CognitiveMemoryCategory:
    """Map any granular or cognitive memory category to its canonical cognitive category."""
    cat_str = category.value if hasattr(category, "value") else str(category).lower()
    mapping = {
        "fact": CognitiveMemoryCategory.SEMANTIC,
        "semantic": CognitiveMemoryCategory.SEMANTIC,
        "knowledge": CognitiveMemoryCategory.SEMANTIC,
        "concept": CognitiveMemoryCategory.SEMANTIC,
        "decision": CognitiveMemoryCategory.EPISODIC,
        "experience": CognitiveMemoryCategory.EPISODIC,
        "interaction": CognitiveMemoryCategory.EPISODIC,
        "session": CognitiveMemoryCategory.EPISODIC,
        "episodic": CognitiveMemoryCategory.EPISODIC,
        "procedure": CognitiveMemoryCategory.PROCEDURAL,
        "lesson": CognitiveMemoryCategory.PROCEDURAL,
        "workflow": CognitiveMemoryCategory.PROCEDURAL,
        "procedural": CognitiveMemoryCategory.PROCEDURAL,
        "preference": CognitiveMemoryCategory.PERSONAL,
        "personal": CognitiveMemoryCategory.PERSONAL,
        "user_profile": CognitiveMemoryCategory.PERSONAL,
        "working": CognitiveMemoryCategory.WORKING,
        "scratchpad": CognitiveMemoryCategory.WORKING,
        "entity": CognitiveMemoryCategory.RELATIONAL,
        "relation": CognitiveMemoryCategory.RELATIONAL,
        "entity_relation": CognitiveMemoryCategory.RELATIONAL,
        "relational": CognitiveMemoryCategory.RELATIONAL,
        "evidence": CognitiveMemoryCategory.SEMANTIC,
    }
    return mapping.get(cat_str, CognitiveMemoryCategory.SEMANTIC)


class MemoryScope(str, Enum):
    """Scope boundaries for knowledge records."""

    GLOBAL = "global"
    PROJECT = "project"
    TASK = "task"
    SESSION = "session"


class MemoryRecord(BaseModel):
    """Canonical structured memory record in SAGE EKM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str = Field(description="Unique stable identifier (UUIDv4).")
    category: MemoryCategory = Field(description="Typed memory record category.")
    scope: MemoryScope = Field(default=MemoryScope.GLOBAL, description="Knowledge scope.")
    project_id: Optional[str] = Field(
        default=None, description="Owning project ID, or None for global."
    )
    task_id: Optional[str] = Field(default=None, description="Owning task ID, if task-scoped.")
    trust_level: MemoryTrustLevel = Field(
        default=MemoryTrustLevel.M0, description="Provenance trust level (M0-M4)."
    )
    title: str = Field(default="", description="Short descriptor or key.")
    content: str = Field(description="Textual knowledge representation.")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Structured attributes.")
    provenance_source: str = Field(
        default="system",
        description="Source channel (conversation, tool, file, web, system, user, experience).",
    )
    evidence_id: Optional[str] = Field(default=None, description="Referenced evidence record ID.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Epistemic confidence.")
    importance: float = Field(default=0.5, ge=0.0, le=1.0, description="Prioritized weight.")
    is_superseded: bool = Field(default=False, description="Whether replaced by newer fact.")
    superseded_by: Optional[str] = Field(default=None, description="ID of successor record.")
    superseded_reason: Optional[str] = Field(
        default=None, description="Rationale for supersession."
    )
    valid_from: str = Field(description="ISO-8601 UTC validity start timestamp.")
    valid_until: Optional[str] = Field(
        default=None, description="ISO-8601 UTC expiration timestamp."
    )
    tags: Tuple[str, ...] = Field(default_factory=tuple, description="Searchable tag attributes.")
    created_at: str = Field(description="ISO-8601 UTC record creation timestamp.")
    updated_at: str = Field(description="ISO-8601 UTC record update timestamp.")


class FactCreate(BaseModel):
    """Payload to create an atomic memory fact in canonical storage."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=256, description="Subject proposition.")
    predicate: str = Field(min_length=1, max_length=128, description="Predicate proposition.")
    object: str = Field(min_length=1, description="Object proposition.")
    category: str = Field(
        default="domain",
        description=(
            "Fact domain category "
            "(domain, user, system, procedure, preference, experience, lesson)."
        ),
    )
    project_id: Optional[str] = Field(default=None, description="Project ID (None = global).")
    trust_level: MemoryTrustLevel = Field(default=MemoryTrustLevel.M1, description="Initial trust.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    provenance_source: str = Field(default="user", description="Provenance origin.")
    evidence_id: Optional[str] = Field(default=None)
    valid_from: Optional[str] = Field(default=None)
    valid_until: Optional[str] = Field(default=None)
    tags: Tuple[str, ...] = Field(default_factory=tuple)


class PreferenceCreate(BaseModel):
    """Payload to create a user preference record."""

    model_config = ConfigDict(extra="forbid")

    domain: str = Field(
        description="Preference domain (coding, communication, review, general, workflow, privacy)."
    )
    preference_key: str = Field(min_length=1, max_length=128)
    preference_value: str = Field(min_length=1)
    preference_type: str = Field(
        default="string", description="Type: string, boolean, number, json."
    )
    project_id: Optional[str] = Field(default=None)
    trust_level: MemoryTrustLevel = Field(default=MemoryTrustLevel.M4)
    source_evidence_id: Optional[str] = Field(default=None)


class EntityCreate(BaseModel):
    """Payload to create an entity node in the knowledge graph."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    canonical_slug: str = Field(min_length=1, max_length=256)
    entity_type: str = Field(
        description=(
            "Entity type "
            "(person, organization, project, concept, file, task, workflow, decision, tool)."
        )
    )
    project_id: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    metadata_json: Optional[str] = Field(default=None)


class RelationCreate(BaseModel):
    """Payload to create a directed relation edge between entities."""

    model_config = ConfigDict(extra="forbid")

    source_entity_id: str
    target_entity_id: str
    relation_type: str = Field(
        description=(
            "Edge type "
            "(depends_on, author_of, references, owns, subconcept_of, related_to, "
            "mentions, contradicts, supports)."
        )
    )
    project_id: Optional[str] = Field(default=None)
    weight: float = Field(default=1.0, ge=0.0)
    trust_level: MemoryTrustLevel = Field(default=MemoryTrustLevel.M2)
    valid_from: Optional[str] = Field(default=None)
    valid_until: Optional[str] = Field(default=None)


class EvidenceCreate(BaseModel):
    """Payload to record source evidence."""

    model_config = ConfigDict(extra="forbid")

    evidence_type: str = Field(
        description=(
            "Evidence type "
            "(conversation_turn, user_confirmed, tool_output, imported_document, "
            "system_observation, external_web)."
        )
    )
    source_reference: str = Field(min_length=1)
    raw_content_snippet: str = Field(min_length=1)
    project_id: Optional[str] = Field(default=None)
    is_tainted: bool = Field(default=False)
    trust_score: float = Field(default=0.5, ge=0.0, le=1.0)


@unique
class LessonState(str, Enum):
    """Lifecycle states of an operational execution lesson."""

    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"


@unique
class LessonRiskLevel(str, Enum):
    """Sensitivity tier determining evidence thresholds required for promotion."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ExecutionLesson(BaseModel):
    """Typed, evidence-linked execution lesson."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lesson_id: str = Field(description="Unique lesson UUID.")
    title: str = Field(min_length=1, max_length=256, description="Short descriptive title.")
    trigger_condition: str = Field(
        min_length=1, description="Condition or task pattern where lesson applies."
    )
    recommended_action: str = Field(
        min_length=1, description="Recommended procedure or avoidance rule."
    )
    observation: str = Field(
        min_length=1, description="Directly observed outcome or telemetry fact."
    )
    inferred_explanation: str = Field(
        min_length=1, description="Inferred hypothesis explaining why the observation occurred."
    )
    skill_id: Optional[str] = Field(default=None, description="Associated skill ID, if applicable.")
    project_id: Optional[str] = Field(default=None, description="Project scope ID, if scoped.")
    state: LessonState = Field(
        default=LessonState.CANDIDATE, description="Current lifecycle state."
    )
    risk_level: LessonRiskLevel = Field(
        default=LessonRiskLevel.MEDIUM, description="Sensitivity classification tier."
    )
    supporting_event_ids: Tuple[str, ...] = Field(
        default_factory=tuple, description="IDs of corroborating ExperienceEvents."
    )
    contradicting_event_ids: Tuple[str, ...] = Field(
        default_factory=tuple, description="IDs of contradicting ExperienceEvents."
    )
    supporting_evidence_ids: Tuple[str, ...] = Field(
        default_factory=tuple, description="IDs of linked EKM memory_evidence records."
    )
    confirmation_count: int = Field(
        default=1, ge=0, description="Number of successful corroborating executions."
    )
    contradiction_count: int = Field(
        default=0, ge=0, description="Number of contradicting failure executions."
    )
    trust_level: MemoryTrustLevel = Field(
        default=MemoryTrustLevel.M1, description="Current provenance trust level."
    )
    fact_id: Optional[str] = Field(
        default=None, description="Linked canonical memory_facts ID when approved."
    )
    is_superseded: bool = Field(default=False, description="Whether replaced by newer lesson.")
    superseded_by: Optional[str] = Field(default=None, description="Successor lesson ID.")
    superseded_reason: Optional[str] = Field(default=None, description="Supersession reason.")
    created_at: str = Field(description="ISO 8601 UTC creation timestamp.")
    updated_at: str = Field(description="ISO 8601 UTC update timestamp.")


class LessonProposal(BaseModel):
    """Payload to propose a new candidate lesson from an execution experience."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    trigger_condition: str = Field(min_length=1)
    recommended_action: str = Field(min_length=1)
    observation: str = Field(min_length=1)
    inferred_explanation: str = Field(min_length=1)
    skill_id: Optional[str] = Field(default=None)
    project_id: Optional[str] = Field(default=None)
    risk_level: LessonRiskLevel = Field(default=LessonRiskLevel.MEDIUM)


class MemoryQuery(BaseModel):
    """Typed query criteria for selective EKM memory retrieval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_text: Optional[str] = Field(default=None, description="Text for keyword/lexical search.")
    categories: Optional[Tuple[MemoryCategory, ...]] = Field(
        default=None, description="Subset of categories to retrieve."
    )
    project_id: Optional[str] = Field(default=None, description="Active project context scope.")
    task_id: Optional[str] = Field(default=None, description="Active task context scope.")
    skill_id: Optional[str] = Field(default=None, description="Associated skill context scope.")
    include_global: bool = Field(default=True, description="Whether to include global records.")
    min_trust_level: Optional[MemoryTrustLevel] = Field(
        default=None, description="Minimum required trust level."
    )
    include_superseded: bool = Field(
        default=False, description="Exclude superseded records unless True."
    )
    include_expired: bool = Field(default=False, description="Exclude expired records unless True.")
    as_of_time: Optional[str] = Field(
        default=None, description="Time anchor for temporal filtering."
    )
    entity_slugs: Optional[Tuple[str, ...]] = Field(
        default=None, description="Filter/expand related to entity slugs."
    )
    limit: int = Field(default=20, ge=1, le=100, description="Maximum candidate records.")
    max_relationship_depth: int = Field(
        default=1, ge=0, le=3, description="Maximum graph traversal depth."
    )
    context_token_budget: Optional[int] = Field(
        default=None, ge=128, le=32768, description="Target token budget for retrieval."
    )


class RetrievalResult(BaseModel):
    """Structured response from selective EKM memory retrieval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    records: List[MemoryRecord] = Field(default_factory=list)
    query: MemoryQuery
    ranking_scores: Dict[str, float] = Field(default_factory=dict)
    provenance_map: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    unresolved_conflicts: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    lessons: List[ExecutionLesson] = Field(
        default_factory=list, description="Retrieved approved execution lessons."
    )


# ==============================================================================
# SAGE UNIFIED MEMORY ENGINE DOMAIN MODELS
# ==============================================================================


class MemoryRelationship(BaseModel):
    """Directed, typed relationship edge between memory records or entities."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    relationship_id: str = Field(
        default_factory=lambda: str(uuid4()), description="Unique relationship UUID."
    )
    source_memory_id: str = Field(default="", description="Origin memory record ID.")
    target_memory_id: str = Field(default="", description="Destination memory record ID.")
    source_id: Optional[str] = Field(default=None, description="Alias for source_memory_id.")
    target_id: Optional[str] = Field(default=None, description="Alias for target_memory_id.")
    relation_type: str = Field(
        default="relates_to",
        description="Relationship type (e.g., associated_with, affects, produced_by, used_in, contradicts, supports, relates_to).",
    )
    relationship_type: Optional[str] = Field(
        default=None, description="Alias for relation_type."
    )
    weight: float = Field(default=1.0, ge=0.0, description="Connection strength weight.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary edge metadata.")
    created_at: str = Field(
        default_factory=_utc_now_iso, description="ISO 8601 UTC creation timestamp."
    )

    def model_post_init(self, __context: Any) -> None:
        object.__setattr__(
            self,
            "source_memory_id",
            self.source_memory_id or self.source_id or "",
        )
        object.__setattr__(
            self,
            "target_memory_id",
            self.target_memory_id or self.target_id or "",
        )
        object.__setattr__(
            self,
            "relation_type",
            self.relationship_type or self.relation_type,
        )


class UnifiedMemoryRecord(BaseModel):
    """Unified memory record spanning semantic, episodic, procedural, personal, working, and relational memories."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    memory_id: str = Field(
        default_factory=lambda: str(uuid4()), description="Unique stable UUID."
    )
    category: MemoryCategory = Field(
        default=MemoryCategory.SEMANTIC, description="Granular memory category."
    )
    cognitive_category: CognitiveMemoryCategory = Field(
        default=CognitiveMemoryCategory.SEMANTIC,
        description="Top-level cognitive memory category.",
    )
    content: str = Field(description="Textual memory representation.")
    summary: Optional[str] = Field(default=None, description="Concise memory summary if compacted.")
    subject: Optional[str] = Field(default=None, description="Subject entity or topic.")
    predicate: Optional[str] = Field(default=None, description="Relationship predicate or property.")
    source: str = Field(
        default="system",
        description="Source channel (conversation, user_explicit, task_execution, tool_result, experience).",
    )
    source_reference: Optional[str] = Field(
        default=None, description="Reference ID of originating task, conversation, or tool."
    )
    provenance_type: MemoryProvenanceType = Field(
        default=MemoryProvenanceType.EXTRACTED, description="Epistemic provenance origin."
    )
    provenance: Optional[Any] = Field(
        default=None, description="Epistemic provenance alias or metadata."
    )
    trust_level: MemoryTrustLevel = Field(
        default=MemoryTrustLevel.M1, description="Provenance trust level (M0-M4)."
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Epistemic confidence.")
    importance: float = Field(default=0.5, ge=0.0, le=1.0, description="Prioritized memory weight.")
    sensitivity: MemorySensitivity = Field(
        default=MemorySensitivity.INTERNAL, description="Security sensitivity classification."
    )
    access_scope: str = Field(
        default="global",
        description="Access boundary (global, project:<id>, contact:<jid>, user:<id>).",
    )
    owner_id: Optional[str] = Field(
        default=None, description="Owning user ID or contact JID for private isolation."
    )
    project_id: Optional[str] = Field(default=None, description="Project scope, if scoped.")
    task_id: Optional[str] = Field(default=None, description="Associated task ID, if scoped.")
    session_id: Optional[str] = Field(default=None, description="Session ID, if scoped.")
    retention_policy: str = Field(
        default="permanent", description="Retention rule (permanent, session, 30d, task)."
    )
    is_superseded: bool = Field(default=False, description="Whether replaced by newer information.")
    superseded_by: Optional[str] = Field(default=None, description="ID of successor record.")
    superseded_reason: Optional[str] = Field(default=None, description="Reason for supersession.")
    valid_from: str = Field(
        default_factory=_utc_now_iso, description="ISO 8601 UTC validity start timestamp."
    )
    valid_until: Optional[str] = Field(default=None, description="ISO 8601 UTC expiration timestamp.")
    expires_at: Optional[str] = Field(default=None, description="Alias for valid_until.")
    tags: Tuple[str, ...] = Field(default_factory=tuple, description="Searchable tag attributes.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary attributes.")
    created_at: str = Field(
        default_factory=_utc_now_iso, description="ISO 8601 UTC creation timestamp."
    )
    updated_at: str = Field(
        default_factory=_utc_now_iso, description="ISO 8601 UTC update timestamp."
    )
    access_count: int = Field(default=0, ge=0, description="Number of times accessed.")

    def model_post_init(self, __context: Any) -> None:
        if self.cognitive_category == CognitiveMemoryCategory.SEMANTIC and self.category != MemoryCategory.SEMANTIC:
            object.__setattr__(self, "cognitive_category", map_to_cognitive_category(self.category))
        if self.expires_at and not self.valid_until:
            object.__setattr__(self, "valid_until", self.expires_at)
        if self.provenance and isinstance(self.provenance, MemoryProvenanceType):
            object.__setattr__(self, "provenance_type", self.provenance)


class UnifiedMemoryQuery(BaseModel):
    """Unified hybrid memory query supporting lexical, semantic, graph, and identity-scoped search."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_text: Optional[str] = Field(default=None, description="Query text for lexical/semantic search.")
    cognitive_categories: Optional[Tuple[CognitiveMemoryCategory, ...]] = Field(
        default=None, description="Filter by one or more cognitive memory categories."
    )
    categories: Optional[Tuple[MemoryCategory, ...]] = Field(
        default=None, description="Filter by granular categories."
    )
    project_id: Optional[str] = Field(default=None, description="Project scope boundary.")
    task_id: Optional[str] = Field(default=None, description="Task scope boundary.")
    session_id: Optional[str] = Field(default=None, description="Session scope boundary.")
    owner_id: Optional[str] = Field(
        default=None, description="Identity isolation filter (contact JID or user ID)."
    )
    access_scope: Optional[str] = Field(
        default=None, description="Access scope match or contact filter."
    )
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    min_importance: float = Field(default=0.0, ge=0.0, le=1.0)
    min_trust_level: Optional[MemoryTrustLevel] = Field(default=None)
    max_sensitivity: Optional[MemorySensitivity] = Field(default=None)
    include_superseded: bool = Field(default=False)
    include_expired: bool = Field(default=False)
    as_of_time: Optional[str] = Field(default=None)
    limit: int = Field(default=20, ge=1, le=100)
    expand_relationships: bool = Field(default=True, description="Whether to traverse connected memory edges.")
    max_relationship_depth: int = Field(default=1, ge=0, le=3)
    context_token_budget: Optional[int] = Field(default=None, ge=128, le=32768)


class UnifiedRetrievalResult(BaseModel):
    """Comprehensive result of hybrid unified memory retrieval."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    records: List[UnifiedMemoryRecord] = Field(default_factory=list)
    query: UnifiedMemoryQuery
    ranking_scores: Dict[str, float] = Field(default_factory=dict)
    relationships: List[MemoryRelationship] = Field(default_factory=list)
    related_records: Dict[str, List[UnifiedMemoryRecord]] = Field(default_factory=dict)
    unresolved_conflicts: List[Dict[str, Any]] = Field(default_factory=list)
    provenance_summary: Dict[str, Any] = Field(default_factory=dict)
    total_found: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def composite_scores(self) -> Dict[str, float]:
        """Alias for ranking_scores."""
        return self.ranking_scores

    @property
    def expanded_relationships(self) -> List[MemoryRelationship]:
        """Alias for relationships."""
        return self.relationships

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, idx: int) -> UnifiedMemoryRecord:
        return self.records[idx]
