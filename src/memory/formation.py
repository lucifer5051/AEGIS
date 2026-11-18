"""SAGE Automatic Memory Formation Pipeline.

Canonical Reference:
- PROMPT 068: SAGE Unified Memory Engine

Lifecycle Pipeline:
    Interaction → Extraction → Classification → Validation → Deduplication → Memory Formation → Persistence

Key Invariants:
1. Never blindly store every message or conversational noise.
2. Explicit user instructions ("remember that...", "keep in mind...") receive top priority (M4, confidence 1.0).
3. Inferences are never silently promoted to verified facts (tagged INFERRED, trust M1, confidence <= 0.6).
4. Conflicting or updated facts supersede existing records rather than creating unmanaged duplicates.
5. Zero secrets: all candidates strictly verified with _check_no_secrets before persistence.
6. Identity and contact isolation preserved (contact JID attached to owner_id and access_scope).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

from src.memory.models import (
    CognitiveMemoryCategory,
    MemoryCategory,
    MemoryProvenanceType,
    MemoryRelationship,
    MemorySensitivity,
    UnifiedMemoryRecord,
    map_to_cognitive_category,
)
from src.memory.unified_store import UnifiedMemoryStore
from src.storage.trust_levels import MemoryTrustLevel
from src.trust.audit import _check_no_secrets
from src.trust.errors import SecretLeakError

logger = logging.getLogger(__name__)

# Patterns for explicit commands
_RE_EXPLICIT_REMEMBER = re.compile(
    r"(?:please\s+)?(?:remember\s+(?:that\s+)?|note\s+(?:that\s+)?|keep\s+in\s+mind\s+(?:that\s+)?|don'?t\s+forget\s+(?:that\s+)?)(.+)",
    re.IGNORECASE,
)

# Patterns for preferences
_RE_PREFERENCE = re.compile(
    r"(?:i\s+prefer\s+|always\s+use\s+|never\s+use\s+|my\s+preference\s+is\s+|i\s+like\s+to\s+use\s+)(.+)",
    re.IGNORECASE,
)

# Patterns for decisions
_RE_DECISION = re.compile(
    r"(?:we\s+decided\s+to\s+|decision:\s*|agreed\s+to\s+|chosen\s+to\s+)(.+)",
    re.IGNORECASE,
)

# Trivial conversational noise phrases to ignore
_NOISE_PHRASES = {
    "hello",
    "hi",
    "hey",
    "thanks",
    "thank you",
    "ok",
    "okay",
    "sounds good",
    "got it",
    "bye",
    "goodbye",
    "yes",
    "no",
    "sure",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CandidateMemory:
    """Intermediate extracted memory proposition before validation and deduplication."""

    content: str
    category: MemoryCategory
    cognitive_category: CognitiveMemoryCategory
    provenance_type: MemoryProvenanceType
    trust_level: MemoryTrustLevel
    confidence: float
    importance: float
    sensitivity: MemorySensitivity = MemorySensitivity.INTERNAL
    source: str = "conversation"
    source_reference: Optional[str] = None
    access_scope: str = "global"
    owner_id: Optional[str] = None
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    tags: Tuple[str, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)
    detected_relationships: List[Tuple[str, str, str]] = field(default_factory=list)  # (src_name, rel_type, tgt_name)


class MemoryFormationEngine:
    """Orchestrates automatic identification, classification, deduplication, and persistence of memories."""

    def __init__(self, store: UnifiedMemoryStore) -> None:
        self._store = store

    # =========================================================================
    # EXTRACTION & CLASSIFICATION
    # =========================================================================

    def extract_candidates(
        self,
        text: str,
        role: str = "user",
        source: str = "conversation",
        source_reference: Optional[str] = None,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        access_scope: Optional[str] = None,
    ) -> List[CandidateMemory]:
        """Extract candidate memories from an interaction turn."""
        cleaned = text.strip()
        if not cleaned:
            return []

        # Filter out pure conversational noise
        if cleaned.lower() in _NOISE_PHRASES:
            return []

        effective_scope = access_scope or ("global" if owner_id is None else f"contact:{owner_id}")
        candidates: List[CandidateMemory] = []

        # 1. Check for Explicit User "Remember" Instruction
        match_remember = _RE_EXPLICIT_REMEMBER.search(cleaned)
        if match_remember:
            core_content = match_remember.group(1).strip()
            if core_content:
                # Classify explicit command: preference vs fact vs rule
                is_pref = bool(_RE_PREFERENCE.search(core_content))
                cat = MemoryCategory.PREFERENCE if is_pref else MemoryCategory.FACT
                cog_cat = CognitiveMemoryCategory.PERSONAL if is_pref else CognitiveMemoryCategory.SEMANTIC

                candidates.append(
                    CandidateMemory(
                        content=core_content,
                        category=cat,
                        cognitive_category=cog_cat,
                        provenance_type=MemoryProvenanceType.EXPLICIT_USER,
                        trust_level=MemoryTrustLevel.M4,
                        confidence=1.0,
                        importance=0.9,
                        sensitivity=MemorySensitivity.INTERNAL,
                        source="user_explicit",
                        source_reference=source_reference,
                        access_scope=effective_scope,
                        owner_id=owner_id,
                        project_id=project_id,
                        task_id=task_id,
                        session_id=session_id,
                        tags=("explicit_user", "priority"),
                    )
                )
                return candidates

        # 2. Check for User Preferences
        match_pref = _RE_PREFERENCE.search(cleaned)
        if match_pref and role == "user":
            core_content = cleaned
            candidates.append(
                CandidateMemory(
                    content=core_content,
                    category=MemoryCategory.PREFERENCE,
                    cognitive_category=CognitiveMemoryCategory.PERSONAL,
                    provenance_type=MemoryProvenanceType.EXTRACTED,
                    trust_level=MemoryTrustLevel.M3,
                    confidence=0.9,
                    importance=0.8,
                    sensitivity=MemorySensitivity.INTERNAL,
                    source=source,
                    source_reference=source_reference,
                    access_scope=effective_scope,
                    owner_id=owner_id,
                    project_id=project_id,
                    task_id=task_id,
                    session_id=session_id,
                    tags=("preference", "user_profile"),
                )
            )
            return candidates

        # 3. Check for Decisions
        match_dec = _RE_DECISION.search(cleaned)
        if match_dec:
            core_content = cleaned
            candidates.append(
                CandidateMemory(
                    content=core_content,
                    category=MemoryCategory.DECISION,
                    cognitive_category=CognitiveMemoryCategory.EPISODIC,
                    provenance_type=MemoryProvenanceType.EXTRACTED,
                    trust_level=MemoryTrustLevel.M3,
                    confidence=0.85,
                    importance=0.8,
                    sensitivity=MemorySensitivity.INTERNAL,
                    source=source,
                    source_reference=source_reference,
                    access_scope=effective_scope,
                    owner_id=owner_id,
                    project_id=project_id,
                    task_id=task_id,
                    session_id=session_id,
                    tags=("decision", "architecture"),
                )
            )
            return candidates

        # 4. Factual and Relational Statements
        # Simple extraction for structured relationships (e.g., "X is associated with Y", "X uses Y")
        rel_pattern = re.search(r"(\b[A-Z][a-zA-Z0-9_-]+\b)\s+(is\s+(?:an?\s+)?(?:lead|architect|manager|part\s+of|used\s+in|author\s+of))\s+(\b[A-Z][a-zA-Z0-9_-]+\b)", cleaned)
        if rel_pattern:
            src_entity, rel_text, tgt_entity = rel_pattern.group(1), rel_pattern.group(2), rel_pattern.group(3)
            candidates.append(
                CandidateMemory(
                    content=cleaned,
                    category=MemoryCategory.RELATION,
                    cognitive_category=CognitiveMemoryCategory.RELATIONAL,
                    provenance_type=MemoryProvenanceType.EXTRACTED,
                    trust_level=MemoryTrustLevel.M2,
                    confidence=0.8,
                    importance=0.7,
                    sensitivity=MemorySensitivity.INTERNAL,
                    source=source,
                    source_reference=source_reference,
                    access_scope=effective_scope,
                    owner_id=owner_id,
                    project_id=project_id,
                    task_id=task_id,
                    session_id=session_id,
                    tags=("relationship", "entity_graph"),
                    detected_relationships=[(src_entity, "relates_to", tgt_entity)],
                )
            )
            return candidates

        # 5. Informative Domain Facts (substantive statements containing tech or project terms)
        if len(cleaned.split()) >= 4 and any(
            kw in cleaned.lower()
            for kw in (
                "database", "server", "api", "key", "config", "port",
                "repository", "protocol", "architecture", "component",
                "service", "storage", "schema", "version", "deployment",
            )
        ):
            candidates.append(
                CandidateMemory(
                    content=cleaned,
                    category=MemoryCategory.FACT,
                    cognitive_category=CognitiveMemoryCategory.SEMANTIC,
                    provenance_type=MemoryProvenanceType.EXTRACTED,
                    trust_level=MemoryTrustLevel.M2,
                    confidence=0.75,
                    importance=0.6,
                    sensitivity=MemorySensitivity.INTERNAL,
                    source=source,
                    source_reference=source_reference,
                    access_scope=effective_scope,
                    owner_id=owner_id,
                    project_id=project_id,
                    task_id=task_id,
                    session_id=session_id,
                    tags=("domain_fact",),
                )
            )

        return candidates

    # =========================================================================
    # VALIDATION, DEDUPLICATION & PERSISTENCE
    # =========================================================================

    def validate_candidate(self, candidate: CandidateMemory) -> bool:
        """Validate candidate proposition against zero-secret and sanity rules."""
        if not candidate.content or not candidate.content.strip():
            return False
        if len(candidate.content) < 3:
            return False

        # Fail closed on detected secrets
        _check_no_secrets(candidate.content)
        for tag in candidate.tags:
            _check_no_secrets(tag)
        return True

    def form_and_persist(self, candidate: CandidateMemory) -> Optional[UnifiedMemoryRecord]:
        """Validate, deduplicate, and persist candidate memory into UnifiedMemoryStore."""
        if not self.validate_candidate(candidate):
            return None

        now = _utc_now_iso()

        # Deduplication and Supersession Check
        # Query existing active memories matching category, owner/project, and lexical overlap
        existing = self._store.list_all(limit=50)
        target_norm = self._normalize_for_match(candidate.content)

        for rec in existing:
            # Same owner/scope check
            if rec.owner_id != candidate.owner_id or rec.project_id != candidate.project_id:
                continue

            rec_norm = self._normalize_for_match(rec.content)

            # Exact match: update recency and boost confidence instead of duplicate
            if rec_norm == target_norm:
                updated = rec.model_copy(
                    update={
                        "confidence": min(1.0, rec.confidence + 0.05),
                        "importance": max(rec.importance, candidate.importance),
                        "access_count": rec.access_count + 1,
                        "updated_at": now,
                    }
                )
                self._store.store_record(updated)
                logger.debug("Deduplicated identical memory '%s', updated recency.", rec.memory_id)
                return updated

            # Conflicting preference/fact update check
            if candidate.category == MemoryCategory.PREFERENCE and rec.category == MemoryCategory.PREFERENCE:
                # If they discuss the same topic/key but different values (e.g. "dark mode" vs "light mode")
                if self._are_conflicting_preferences(rec.content, candidate.content):
                    new_id = str(uuid4())
                    new_record = self._candidate_to_record(candidate, new_id, now)
                    self._store.store_record(new_record)
                    self._store.supersede_record(
                        old_memory_id=rec.memory_id,
                        new_memory_id=new_id,
                        reason="superseded_by_newer_preference",
                    )
                    logger.info("Superseded older preference '%s' with '%s'", rec.memory_id, new_id)
                    return new_record

        # New unique memory record
        new_id = str(uuid4())
        record = self._candidate_to_record(candidate, new_id, now)
        self._store.store_record(record)

        # Record relationships if detected
        if candidate.detected_relationships:
            for src_name, rel_type, tgt_name in candidate.detected_relationships:
                rel = MemoryRelationship(
                    relationship_id=str(uuid4()),
                    source_memory_id=record.memory_id,
                    target_memory_id=record.memory_id,  # Linked to self or target record
                    relation_type=rel_type,
                    weight=1.0,
                    metadata={"source_entity": src_name, "target_entity": tgt_name},
                    created_at=now,
                )
                self._store.add_relationship(rel)

        return record

    def process_interaction(
        self,
        text: str,
        role: str = "user",
        source: str = "conversation",
        source_reference: Optional[str] = None,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        access_scope: Optional[str] = None,
    ) -> List[UnifiedMemoryRecord]:
        """Convenience method: extract candidates, deduplicate, and persist all formed memories."""
        candidates = self.extract_candidates(
            text=text,
            role=role,
            source=source,
            source_reference=source_reference,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            owner_id=owner_id,
            access_scope=access_scope,
        )
        persisted = []
        for cand in candidates:
            rec = self.form_and_persist(cand)
            if rec:
                persisted.append(rec)
        return persisted

    def form_from_interaction(
        self,
        user_text: str,
        assistant_response: Optional[str] = None,
        actor: str = "user",
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        access_scope: Optional[str] = None,
    ) -> List[UnifiedMemoryRecord]:
        """Convenience alias for multi-turn or user-facing interaction memory formation."""
        return self.process_interaction(
            text=user_text,
            role="user",
            source="conversation",
            source_reference=session_id or task_id,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            owner_id=owner_id or (actor if actor != "user" else None),
            access_scope=access_scope,
        )

    def record_task_outcome(
        self,
        task_id: str,
        goal: str,
        success: bool,
        synthesis: str = "",
        error: Optional[str] = None,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[UnifiedMemoryRecord]:
        """Form an episodic or procedural memory from a completed task execution outcome."""
        content = f"Task '{goal}' outcome: {'SUCCESS' if success else 'FAILURE'}. {synthesis}"
        if error:
            content += f" Error: {error}"

        cog_cat = CognitiveMemoryCategory.PROCEDURAL if success else CognitiveMemoryCategory.EPISODIC
        cat = MemoryCategory.PROCEDURE if success else MemoryCategory.EXPERIENCE

        cand = CandidateMemory(
            content=content,
            category=cat,
            cognitive_category=cog_cat,
            provenance_type=MemoryProvenanceType.EXECUTION,
            trust_level=MemoryTrustLevel.M3,
            confidence=0.95,
            importance=0.7 if success else 0.85,
            sensitivity=MemorySensitivity.INTERNAL,
            source="task_execution",
            source_reference=task_id,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            tags=("task_outcome", "execution"),
        )
        return self.form_and_persist(cand)

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _candidate_to_record(
        self,
        c: CandidateMemory,
        memory_id: str,
        now: str,
    ) -> UnifiedMemoryRecord:
        return UnifiedMemoryRecord(
            memory_id=memory_id,
            category=c.category,
            cognitive_category=c.cognitive_category,
            content=c.content,
            summary=None,
            source=c.source,
            source_reference=c.source_reference,
            provenance_type=c.provenance_type,
            trust_level=c.trust_level,
            confidence=c.confidence,
            importance=c.importance,
            sensitivity=c.sensitivity,
            access_scope=c.access_scope,
            owner_id=c.owner_id,
            project_id=c.project_id,
            task_id=c.task_id,
            session_id=c.session_id,
            retention_policy="permanent",
            is_superseded=False,
            superseded_by=None,
            superseded_reason=None,
            valid_from=now,
            valid_until=None,
            tags=c.tags,
            metadata=c.metadata,
            created_at=now,
            updated_at=now,
        )

    def _normalize_for_match(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().strip())

    def _are_conflicting_preferences(self, old_text: str, new_text: str) -> bool:
        """Heuristic to detect if two preference propositions address opposing values."""
        t1, t2 = old_text.lower(), new_text.lower()
        conflict_pairs = [
            ("dark", "light"),
            ("always", "never"),
            ("tabs", "spaces"),
            ("json", "yaml"),
            ("python", "rust"),
            ("enable", "disable"),
            ("verbose", "terse"),
            ("concise", "detailed"),
        ]
        for a, b in conflict_pairs:
            if (a in t1 and b in t2) or (b in t1 and a in t2):
                return True
        return False
