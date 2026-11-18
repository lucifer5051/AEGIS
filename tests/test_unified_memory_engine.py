"""SAGE Unified Memory Engine Test Suite.

Canonical Reference:
    PROMPT 068: SAGE Unified Memory Engine -- Automatic Memory Formation, Retrieval & Context Integration

Tests all core Unified Memory Engine subsystems:
1. Unified memory categories (Semantic, Episodic, Procedural, Personal, Working, Relational).
2. Automatic memory formation pipeline (propositions, classification, noise filtering, provenance).
3. Deduplication and supersession chains.
4. Transactional SQLite persistence & FTS5 virtual table synchronization.
5. Hybrid retrieval pipeline (structured filters, lexical FTS5, 100% offline local semantic similarity, graph expansion).
6. Multi-factor composite ranking and conflict detection.
7. Friday/WhatsApp contact isolation and zero-secret boundary enforcement.
8. Context integration (MemoryContextBuilder, token budgets, untrusted boundaries).
9. Memory lifecycle operations (remember, recall, forget, consolidate).
10. Supervisor, ExperienceFeedbackService, and ContextAssembler integration.
"""

from __future__ import annotations

import sqlite3
import pytest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from src.memory.models import (
    CognitiveMemoryCategory,
    MemoryCategory,
    MemoryProvenanceType,
    MemoryRelationship,
    MemorySensitivity,
    UnifiedMemoryQuery,
    UnifiedMemoryRecord,
    map_to_cognitive_category,
)
from src.memory.unified_store import UnifiedMemoryStore
from src.memory.formation import MemoryFormationEngine
from src.memory.hybrid_retriever import HybridMemoryRetriever, LocalSemanticAdapter
from src.memory.context_builder import MemoryContextBuilder
from src.memory.engine import UnifiedMemoryEngine
from src.storage.trust_levels import MemoryTrustLevel
from src.manager.context import ContextAssembler, ContextIR
from src.skills.experience import (
    ExperienceEvent,
    ExperienceFeedbackService,
    ExperienceOutcomeType,
)


@pytest.fixture
def in_memory_conn():
    """Create an in-memory SQLite database connection."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def memory_engine(in_memory_conn):
    """Instantiate a UnifiedMemoryEngine backed by an in-memory database."""
    return UnifiedMemoryEngine(in_memory_conn)


# ==============================================================================
# 1. CATEGORY & MODEL MAPPING TESTS
# ==============================================================================


class TestMemoryCategoryMapping:
    """Verify that granular categories map properly to the 6 cognitive categories."""

    def test_cognitive_category_mappings(self):
        assert map_to_cognitive_category(MemoryCategory.FACT) == CognitiveMemoryCategory.SEMANTIC
        assert map_to_cognitive_category(MemoryCategory.KNOWLEDGE) == CognitiveMemoryCategory.SEMANTIC
        assert map_to_cognitive_category(MemoryCategory.CONCEPT) == CognitiveMemoryCategory.SEMANTIC

        assert map_to_cognitive_category(MemoryCategory.EPISODIC) == CognitiveMemoryCategory.EPISODIC
        assert map_to_cognitive_category(MemoryCategory.INTERACTION) == CognitiveMemoryCategory.EPISODIC
        assert map_to_cognitive_category(MemoryCategory.DECISION) == CognitiveMemoryCategory.EPISODIC

        assert map_to_cognitive_category(MemoryCategory.PROCEDURE) == CognitiveMemoryCategory.PROCEDURAL
        assert map_to_cognitive_category(MemoryCategory.LESSON) == CognitiveMemoryCategory.PROCEDURAL
        assert map_to_cognitive_category(MemoryCategory.WORKFLOW) == CognitiveMemoryCategory.PROCEDURAL

        assert map_to_cognitive_category(MemoryCategory.PREFERENCE) == CognitiveMemoryCategory.PERSONAL
        assert map_to_cognitive_category(MemoryCategory.USER_PROFILE) == CognitiveMemoryCategory.PERSONAL

        assert map_to_cognitive_category(MemoryCategory.WORKING) == CognitiveMemoryCategory.WORKING
        assert map_to_cognitive_category(MemoryCategory.SCRATCHPAD) == CognitiveMemoryCategory.WORKING

        assert map_to_cognitive_category(MemoryCategory.RELATIONAL) == CognitiveMemoryCategory.RELATIONAL
        assert map_to_cognitive_category(MemoryCategory.ENTITY_RELATION) == CognitiveMemoryCategory.RELATIONAL

    def test_record_validation_and_defaults(self):
        rec = UnifiedMemoryRecord(
            category=MemoryCategory.SEMANTIC,
            content="SQLite supports full-text search with FTS5.",
            subject="SQLite",
        )
        assert rec.memory_id
        assert rec.confidence == 1.0
        assert rec.trust_level in (MemoryTrustLevel.M1, "M1")
        assert rec.sensitivity == MemorySensitivity.INTERNAL
        assert not rec.is_superseded


# ==============================================================================
# 2. STORAGE & PERSISTENCE TESTS
# ==============================================================================


class TestUnifiedMemoryStore:
    """Verify transactional persistence, FTS5 sync, and supersession chains."""

    def test_store_and_get_record(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        rec = UnifiedMemoryRecord(
            category=MemoryCategory.SEMANTIC,
            content="Python 3.11 introduced TaskGroup for asyncio.",
            subject="Python",
            predicate="supports",
            confidence=0.95,
            project_id="aegis_core",
        )
        created_id = store.store_record(rec)
        assert created_id == rec.memory_id

        fetched = store.get_record(rec.memory_id)
        assert fetched is not None
        assert fetched.content == "Python 3.11 introduced TaskGroup for asyncio."
        assert fetched.subject == "Python"
        assert fetched.confidence == 0.95

    def test_fts5_lexical_search(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        store.store_record(
            UnifiedMemoryRecord(
                category=MemoryCategory.SEMANTIC,
                content="PostgreSQL offers advanced JSON indexing.",
                subject="PostgreSQL",
            )
        )
        store.store_record(
            UnifiedMemoryRecord(
                category=MemoryCategory.SEMANTIC,
                content="Redis is an in-memory key-value data structure store.",
                subject="Redis",
            )
        )

        matches = store.search_fts("PostgreSQL JSON", limit=5)
        assert len(matches) >= 1
        rec_ids = [m[0] for m in matches]
        first_rec = store.get_record(rec_ids[0])
        assert first_rec is not None
        assert "PostgreSQL" in first_rec.content

    def test_supersession_chain(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        old_rec = UnifiedMemoryRecord(
            category=MemoryCategory.PERSONAL,
            content="User prefers light theme.",
            subject="User",
            predicate="prefers_theme",
        )
        old_id = store.store_record(old_rec)

        new_rec = UnifiedMemoryRecord(
            category=MemoryCategory.PERSONAL,
            content="User prefers dark theme.",
            subject="User",
            predicate="prefers_theme",
        )
        superseded_id = store.supersede_record(old_id, new_rec)

        assert superseded_id != old_id
        new_fetched = store.get_record(superseded_id)
        assert new_fetched is not None
        assert new_fetched.content == "User prefers dark theme."

        old_fetched = store.get_record(old_id)
        assert old_fetched is not None
        assert old_fetched.is_superseded is True
        assert old_fetched.superseded_by == superseded_id

    def test_soft_and_hard_delete(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        rec = UnifiedMemoryRecord(
            category=MemoryCategory.WORKING,
            content="Temporary calculation step.",
        )
        rec_id = store.store_record(rec)

        # Soft delete
        assert store.delete_record(rec_id, hard_delete=False) is True
        fetched = store.get_record(rec_id)
        assert fetched is not None
        assert fetched.is_superseded is True

        # Hard delete
        assert store.delete_record(rec_id, hard_delete=True) is True
        assert store.get_record(rec_id) is None


# ==============================================================================
# 3. AUTOMATIC MEMORY FORMATION TESTS
# ==============================================================================


class TestMemoryFormationEngine:
    """Verify proposition extraction, noise filtering, and deduplication."""

    def test_explicit_user_remember_instruction(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        engine = MemoryFormationEngine(store)

        text = "Please remember that our main production database host is db.internal.net."
        records = engine.form_from_interaction(
            user_text=text,
            assistant_response="Understood, I will remember that.",
            actor="digvijay",
        )
        assert len(records) >= 1
        rec = records[0]
        assert rec.provenance_type == MemoryProvenanceType.EXPLICIT_USER
        assert rec.trust_level in (MemoryTrustLevel.M4, "M4")
        assert rec.confidence == 1.0
        assert "db.internal.net" in rec.content

    def test_user_preference_extraction(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        engine = MemoryFormationEngine(store)

        text = "I prefer using strict type annotations in all Python modules."
        records = engine.form_from_interaction(
            user_text=text,
            assistant_response="Noted.",
            actor="digvijay",
        )
        assert len(records) >= 1
        pref_mem = next(
            (
                r
                for r in records
                if r.cognitive_category == CognitiveMemoryCategory.PERSONAL
                or r.category in (MemoryCategory.PERSONAL, MemoryCategory.PREFERENCE)
            ),
            None,
        )
        assert pref_mem is not None
        assert "strict type annotations" in pref_mem.content

    def test_conversational_noise_filtering(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        engine = MemoryFormationEngine(store)

        noise_inputs = [
            "hello there",
            "thank you very much",
            "okay got it",
            "yes",
            "bye!",
        ]
        for noise in noise_inputs:
            records = engine.form_from_interaction(
                user_text=noise,
                assistant_response="You're welcome!",
            )
            assert len(records) == 0, f"Expected no memories formed for noise: {noise}"

    def test_zero_secret_rejection(self, in_memory_conn):
        from src.trust.errors import SecretLeakError

        store = UnifiedMemoryStore(in_memory_conn)
        engine = MemoryFormationEngine(store)

        secret_text = 'Please remember that my api_key: "sk-ant-api03-1234567890abcdef".'
        with pytest.raises((ValueError, SecretLeakError)):
            engine.form_from_interaction(user_text=secret_text)

    def test_deduplication_updates_recency(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        engine = MemoryFormationEngine(store)

        text = "Remember that the deployment target is AWS us-east-1."
        first_records = engine.form_from_interaction(user_text=text)
        assert len(first_records) == 1
        first_id = first_records[0].memory_id
        initial_access_count = first_records[0].access_count

        # Repeat identical interaction
        second_records = engine.form_from_interaction(user_text=text)
        assert len(second_records) == 1
        assert second_records[0].memory_id == first_id
        assert second_records[0].access_count > initial_access_count


# ==============================================================================
# 4. HYBRID RETRIEVAL & LOCAL SEMANTIC ADAPTER TESTS
# ==============================================================================


class TestHybridRetrievalAndRanking:
    """Verify 100% offline local semantic adapter, FTS5 lexical, and composite ranking."""

    def test_local_semantic_similarity(self):
        adapter = LocalSemanticAdapter(ngram_range=(1, 2))
        score_high = adapter.similarity(
            "SQLite database with encrypted storage",
            "Encrypted SQLite database storage system",
        )
        score_low = adapter.similarity(
            "SQLite database with encrypted storage",
            "Baking chocolate chip cookies in an oven",
        )
        assert score_high > 0.4
        assert score_low < 0.1
        assert score_high > score_low

    def test_hybrid_retrieval_and_ranking(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        retriever = HybridMemoryRetriever(store)

        # Store test memories
        store.store_record(
            UnifiedMemoryRecord(
                category=MemoryCategory.SEMANTIC,
                content="Architecture uses SQLCipher for on-disk encrypted database storage.",
                subject="Architecture",
                importance=0.9,
                confidence=1.0,
            )
        )
        store.store_record(
            UnifiedMemoryRecord(
                category=MemoryCategory.PROCEDURAL,
                content="Run tests completely offline using the designated virtual environment.",
                subject="Testing",
                importance=0.8,
                confidence=1.0,
            )
        )
        store.store_record(
            UnifiedMemoryRecord(
                category=MemoryCategory.PERSONAL,
                content="User prefers concise answers without fluff.",
                subject="User",
                importance=0.7,
                confidence=1.0,
            )
        )

        query = UnifiedMemoryQuery(
            query_text="encrypted database storage",
            limit=3,
        )
        result = retriever.retrieve(query)
        assert len(result.records) > 0
        top_rec = result.records[0]
        assert "SQLCipher" in top_rec.content
        assert result.composite_scores[top_rec.memory_id] > 0.0

    def test_relationship_graph_expansion(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        retriever = HybridMemoryRetriever(store)

        # Create entity 1: Project SAGE
        sage = UnifiedMemoryRecord(
            category=MemoryCategory.SEMANTIC,
            content="SAGE is the autonomous guidance and execution system.",
            subject="SAGE",
        )
        sage_id = store.store_record(sage)
        # Create entity 2: Subsystem TrustCore
        trustcore = UnifiedMemoryRecord(
            category=MemoryCategory.SEMANTIC,
            content="TrustCore enforces unbypassable deterministic authorization.",
            subject="TrustCore",
        )
        trustcore_id = store.store_record(trustcore)
        # Create relationship: SAGE contains TrustCore
        store.add_relationship(
            MemoryRelationship(
                source_id=sage_id,
                target_id=trustcore_id,
                relationship_type="CONTAINS_SUBSYSTEM",
            )
        )

        query = UnifiedMemoryQuery(
            query_text="SAGE autonomous guidance",
            expand_relationships=True,
            max_relationship_depth=1,
            limit=5,
        )
        result = retriever.retrieve(query)
        rec_ids = [r.memory_id for r in result.records]
        assert sage_id in rec_ids
        assert trustcore_id in rec_ids
        assert len(result.expanded_relationships) >= 1


# ==============================================================================
# 5. PRIVACY, SENSITIVITY & CONTACT ISOLATION TESTS (FRIDAY INVARIANT)
# ==============================================================================


class TestContactMemoryIsolation:
    """Verify strict privacy boundaries for communication channels (WhatsApp/Friday)."""

    def test_contact_isolation_boundary(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        retriever = HybridMemoryRetriever(store)

        # Store private memory for Alice
        alice_mem = UnifiedMemoryRecord(
            category=MemoryCategory.PERSONAL,
            content="Alice prefers meetings scheduled in the morning.",
            owner_id="contact:+1111111111",
            access_scope="contact:+1111111111",
        )
        alice_id = store.store_record(alice_mem)

        # Store private memory for Bob
        bob_mem = UnifiedMemoryRecord(
            category=MemoryCategory.PERSONAL,
            content="Bob prefers asynchronous communication only.",
            owner_id="contact:+2222222222",
            access_scope="contact:+2222222222",
        )
        bob_id = store.store_record(bob_mem)

        # Store global organizational fact
        global_mem = UnifiedMemoryRecord(
            category=MemoryCategory.SEMANTIC,
            content="The office core hours are 10am to 4pm.",
            access_scope="global",
        )
        global_id = store.store_record(global_mem)

        # Query on behalf of Alice
        alice_query = UnifiedMemoryQuery(
            query_text="prefers communication meetings hours",
            owner_id="contact:+1111111111",
            access_scope="contact:+1111111111",
        )
        alice_res = retriever.retrieve(alice_query)
        alice_ids = [r.memory_id for r in alice_res.records]

        assert alice_id in alice_ids
        assert global_id in alice_ids
        assert bob_id not in alice_ids, "Alice should never access Bob's private memory!"

        # Query on behalf of Bob
        bob_query = UnifiedMemoryQuery(
            query_text="prefers communication meetings hours",
            owner_id="contact:+2222222222",
            access_scope="contact:+2222222222",
        )
        bob_res = retriever.retrieve(bob_query)
        bob_ids = [r.memory_id for r in bob_res.records]

        assert bob_id in bob_ids
        assert global_id in bob_ids
        assert alice_id not in bob_ids, "Bob should never access Alice's private memory!"


# ==============================================================================
# 6. CONTEXT BUILDER & TOKEN BUDGETING TESTS
# ==============================================================================


class TestMemoryContextBuilder:
    """Verify prompt formatting, token estimation, and section organization."""

    def test_context_builder_budget_and_boundaries(self, in_memory_conn):
        store = UnifiedMemoryStore(in_memory_conn)
        builder = MemoryContextBuilder(store)

        records = [
            UnifiedMemoryRecord(
                category=MemoryCategory.PERSONAL,
                content="Prefers Python for system implementation.",
                subject="Language Preference",
                confidence=1.0,
                provenance=MemoryProvenanceType.EXPLICIT_USER,
            ),
            UnifiedMemoryRecord(
                category=MemoryCategory.PROCEDURAL,
                content="Always check offline test compatibility before finishing.",
                subject="Testing Procedure",
                confidence=0.9,
            ),
            UnifiedMemoryRecord(
                category=MemoryCategory.EPISODIC,
                content="Decided to remediate ARCH-060-07 via checkpoint hierarchy.",
                subject="Decision ARCH-060-07",
                confidence=0.9,
            ),
        ]

        result = builder.build_context(records=records, token_budget=500)
        assert result.rendered_text
        assert "[BEGIN UNTRUSTED MEMORY CONTEXT]" in result.rendered_text
        assert "[END UNTRUSTED MEMORY CONTEXT]" in result.rendered_text
        assert "VERIFIED PREFERENCES & FACTS" in result.rendered_text
        assert "KNOWN PROCEDURES & LESSONS" in result.rendered_text
        assert result.token_count <= 500
        assert len(result.included_memory_ids) == 3


# ==============================================================================
# 7. UNIFIED MEMORY ENGINE HIGH-LEVEL OPERATIONS
# ==============================================================================


class TestUnifiedMemoryEngineOperations:
    """Verify remember, recall, forget, consolidate, and task outcome recording."""

    def test_remember_recall_forget_cycle(self, memory_engine):
        # Remember
        record = memory_engine.remember(
            content="FastDecisionService is advisory only; TrustCore is authoritative.",
            category=MemoryCategory.SEMANTIC,
            subject="FastDecisionService",
            importance=1.0,
            confidence=1.0,
        )
        assert record.memory_id

        # Recall
        recall_res = memory_engine.recall(
            query_text="Is FastDecisionService authoritative?",
            limit=2,
        )
        assert len(recall_res.records) >= 1
        assert recall_res.records[0].memory_id == record.memory_id

        # Forget (soft delete)
        success = memory_engine.forget(record.memory_id, hard_delete=False)
        assert success is True

        # Recall again -> should not return forgotten record
        recall_after = memory_engine.recall(
            query_text="Is FastDecisionService authoritative?",
            limit=2,
        )
        active_ids = [r.memory_id for r in recall_after.records if not r.is_superseded]
        assert record.memory_id not in active_ids

    def test_record_task_outcome(self, memory_engine):
        records = memory_engine.record_task_outcome(
            task_id="task-999",
            intent="Compile sqlite3 extension offline",
            success=True,
            details="Compiled successfully using gcc with -O2 optimization.",
            project_id="aegis_core",
        )
        assert len(records) >= 1
        outcome_rec = next(
            (
                r
                for r in records
                if r.cognitive_category
                in (CognitiveMemoryCategory.PROCEDURAL, CognitiveMemoryCategory.EPISODIC)
            ),
            None,
        )
        assert outcome_rec is not None
        assert "Compile sqlite3 extension offline" in outcome_rec.content
        assert outcome_rec.project_id == "aegis_core"

    def test_consolidate_memories_pruning(self, memory_engine):
        # Insert expired working memory
        old_working = UnifiedMemoryRecord(
            category=MemoryCategory.WORKING,
            content="Temporary scratch buffer from previous run.",
            expires_at="2020-01-01T00:00:00Z",
        )
        memory_engine.store.store_record(old_working)

        # Consolidate
        stats = memory_engine.consolidate_memories()
        assert stats["expired_pruned"] >= 1


# ==============================================================================
# 8. SUPERVISOR & EXPERIENCE INTEGRATION TESTS
# ==============================================================================


class TestUnifiedMemorySupervisorIntegration:
    """Verify integration of UnifiedMemoryEngine with ExperienceFeedbackService and ContextAssembler."""

    def test_experience_service_records_to_unified_memory(self, in_memory_conn):
        engine = UnifiedMemoryEngine(in_memory_conn)
        exp_service = ExperienceFeedbackService(
            conn=in_memory_conn,
            unified_memory=engine,
        )

        event = ExperienceEvent(
            task_id="task-1001",
            invocation_id="inv-1001",
            skill_id="test_skill",
            intent="Execute offline unit test",
            selected_tools=("test_tool",),
            execution_path=("test_path",),
            verification_result="VERIFIED",
            outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
            latency_ms=120.5,
            confidence=1.0,
            provenance="test_harness",
        )
        exp_service.record_event(event)

        # Recall from unified memory
        recalled = engine.recall(query_text="Execute offline unit test")
        assert len(recalled.records) >= 1
        assert any("test_skill" in r.content for r in recalled.records)

    def test_context_assembler_augments_from_unified_memory(self, in_memory_conn):
        engine = UnifiedMemoryEngine(in_memory_conn)
        # Store a personal preference in unified memory
        engine.remember(
            content="User prefers minimal diffs and concise responses.",
            category=MemoryCategory.PERSONAL,
            subject="Coding Style",
            importance=0.9,
        )

        assembler = ContextAssembler(
            conn=in_memory_conn,
            unified_memory=engine,
        )

        context_ir = assembler.assemble(goal="Refactor memory module")
        assert any("Coding Style" in p or "concise responses" in p for p in context_ir.relevant_preferences)
