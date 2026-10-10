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

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from src.storage.trust_levels import MemoryTrustLevel


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


class MemoryQuery(BaseModel):
    """Typed query criteria for selective EKM memory retrieval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_text: Optional[str] = Field(default=None, description="Text for keyword/lexical search.")
    categories: Optional[Tuple[MemoryCategory, ...]] = Field(
        default=None, description="Subset of categories to retrieve."
    )
    project_id: Optional[str] = Field(default=None, description="Active project context scope.")
    task_id: Optional[str] = Field(default=None, description="Active task context scope.")
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
