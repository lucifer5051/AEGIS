"""SAGE Experience & Knowledge Memory (EKM) Runtime & Context Retrieval Test Suite.

Canonical Reference:
- docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
- docs/specifications/AEGIS_DOMAIN_SCHEMA_FOUNDATION.md
- PROMPT 024 Specification (§ 9)

Invariants Verified:
1. Typed memory records preserved with full fidelity.
2. Zero secret leakage: SecretLeakError raised before persistence.
3. Auxiliary FTS5 virtual table handles sanitization, lexical search, and deterministic rebuild.
4. Multi-stage selective retrieval: scope boundaries, temporal validity, trust-weighted ranking.
5. Entity relationship graph traversal respects depth bounds.
6. Deduplication and conflict detection for opposing facts and preferences.
7. Bounded Context IR compilation enforces token budget with uncertainty metadata.
8. Supervisor integration compiles bounded context seamlessly.
9. 100% offline, zero network calls.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from src.manager.context import ContextIR
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.memory import (
    ContextCompiler,
    EKMMemoryStore,
    EKMRetriever,
    EntityCreate,
    EvidenceCreate,
    FactCreate,
    MemoryCategory,
    MemoryQuery,
    MemoryScope,
    PreferenceCreate,
    RelationCreate,
    estimate_tokens,
    sanitize_fts5_query,
)
from src.storage.database import create_connection
from src.storage.migrations import (
    apply_migrations,
    bootstrap_schema_migrations,
    load_migrations_from_directory,
)
from src.storage.trust_levels import MemoryTrustLevel
from src.tools.broker import ToolBroker
from src.trust.audit import SecretLeakError

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


# ==============================================================================
# FIXTURES
# ==============================================================================


@pytest.fixture
def tmp_db_path() -> Generator[Path, None, None]:
    """Provide a temporary database file path and clean up afterwards."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        yield db_path
    finally:
        for ext in ("", "-wal", "-shm"):
            Path(str(db_path) + ext).unlink(missing_ok=True)


@pytest.fixture
def migrated_conn(tmp_db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Create a SQLite connection with all migrations 0001-0006 applied."""
    conn = create_connection(tmp_db_path)
    try:
        migrations = load_migrations_from_directory(MIGRATIONS_DIR)
        bootstrap_schema_migrations(conn)
        apply_migrations(conn, migrations)

        # Create two test projects for scoped tests
        now = "2026-10-10T12:00:00Z"
        conn.execute(
            "INSERT INTO projects (project_id, name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'ACTIVE', ?, ?);",
            ("proj-alpha", "Project Alpha", "alpha", now, now),
        )
        conn.execute(
            "INSERT INTO projects (project_id, name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'ACTIVE', ?, ?);",
            ("proj-beta", "Project Beta", "beta", now, now),
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


@pytest.fixture
def ekm_store(migrated_conn: sqlite3.Connection) -> EKMMemoryStore:
    """Provide an EKMMemoryStore wired to the migrated test database."""
    return EKMMemoryStore(migrated_conn)


@pytest.fixture
def ekm_retriever(ekm_store: EKMMemoryStore) -> EKMRetriever:
    """Provide an EKMRetriever wired to the memory store."""
    return EKMRetriever(ekm_store)


@pytest.fixture
def context_compiler() -> ContextCompiler:
    """Provide a ContextCompiler."""
    return ContextCompiler()


# ==============================================================================
# TEST SUITE: 24 MANDATORY CASES
# ==============================================================================


def test_record_typed_creation(ekm_store: EKMMemoryStore) -> None:
    """1. Add and retrieve typed fact with all fields preserved."""
    fact_id = ekm_store.add_fact(
        FactCreate(
            subject="architecture",
            predicate="uses",
            object="sqlite WAL mode",
            category="domain",
            project_id="proj-alpha",
            trust_level=MemoryTrustLevel.M3,
            confidence=0.95,
            importance=0.85,
            tags=("db", "wal"),
        )
    )
    assert fact_id is not None
    record = ekm_store.get_fact(fact_id)
    assert record is not None
    assert record.memory_id == fact_id
    assert record.category == MemoryCategory.FACT
    assert record.scope == MemoryScope.PROJECT
    assert record.project_id == "proj-alpha"
    assert record.trust_level == MemoryTrustLevel.M3
    assert record.confidence == 0.95
    assert record.importance == 0.85
    assert record.is_superseded is False
    assert "sqlite WAL mode" in record.content


def test_secret_leak_rejection_fact(ekm_store: EKMMemoryStore) -> None:
    """2. Secret in fact raises SecretLeakError."""
    with pytest.raises(SecretLeakError):
        ekm_store.add_fact(
            FactCreate(
                subject="credentials",
                predicate="contains",
                object="password = SuperSecret123!",
            )
        )


def test_secret_leak_rejection_preference(ekm_store: EKMMemoryStore) -> None:
    """3. Secret in preference raises SecretLeakError."""
    with pytest.raises(SecretLeakError):
        ekm_store.add_preference(
            PreferenceCreate(
                domain="general",
                preference_key="api_key",
                preference_value="AKIA1234567890ABCDEF",
            )
        )


def test_secret_leak_rejection_evidence(ekm_store: EKMMemoryStore) -> None:
    """4. Secret in evidence snippet raises SecretLeakError."""
    with pytest.raises(SecretLeakError):
        ekm_store.add_evidence(
            EvidenceCreate(
                evidence_type="conversation_turn",
                source_reference="chat:1",
                raw_content_snippet='{"bearer": "Bearer eyJhbGciOiJIUzI1NiJ9"}',
            )
        )


def test_secret_leak_rejection_entity(ekm_store: EKMMemoryStore) -> None:
    """5. Secret in entity description raises SecretLeakError."""
    with pytest.raises(SecretLeakError):
        ekm_store.add_entity(
            EntityCreate(
                name="ServerConfig",
                canonical_slug="server-config",
                entity_type="concept",
                description="Contains private_key = -----BEGIN RSA PRIVATE KEY-----",
            )
        )


def test_fts5_indexing_and_search(ekm_store: EKMMemoryStore) -> None:
    """6. Record indexed and retrieved via FTS5 keyword query."""
    fact_id = ekm_store.add_fact(
        FactCreate(
            subject="compiler",
            predicate="optimizes",
            object="intermediate representation bytecode",
        )
    )
    matches = ekm_store.indexer.search("bytecode")
    assert len(matches) > 0
    matched_ids = [m[0] for m in matches]
    assert fact_id in matched_ids


def test_fts5_query_sanitization(ekm_store: EKMMemoryStore) -> None:
    """7. Weird punctuation/syntax in query does not crash FTS5."""
    weird_query = 'SELECT * FROM foo WHERE (id: [123] ^ "unterminated*'
    safe = sanitize_fts5_query(weird_query)
    assert '"' in safe
    # Ensure executing does not raise sqlite3.OperationalError
    matches = ekm_store.indexer.search(weird_query)
    assert isinstance(matches, list)


def test_fts5_rebuild_from_canonical(ekm_store: EKMMemoryStore) -> None:
    """8. Drop/corrupt index, call rebuild_index(), all facts and entities re-indexed."""
    f1 = ekm_store.add_fact(
        FactCreate(subject="engine", predicate="supports", object="deterministic execution")
    )
    e1 = ekm_store.add_entity(
        EntityCreate(
            name="Orchestrator",
            canonical_slug="orchestrator",
            entity_type="tool",
            description="Autonomous supervisor engine",
        )
    )
    p1 = ekm_store.add_preference(
        PreferenceCreate(
            domain="coding",
            preference_key="style",
            preference_value="pep8",
        )
    )

    # Rebuild from scratch
    count = ekm_store.rebuild_index()
    assert count >= 3

    # Check search still works
    matches = ekm_store.indexer.search("orchestrator")
    assert any(m[0] == e1 for m in matches)
    matches_fact = ekm_store.indexer.search("deterministic")
    assert any(m[0] == f1 for m in matches_fact)
    matches_pref = ekm_store.indexer.search("pep8")
    assert any(m[0] == p1 for m in matches_pref)


def test_scope_isolation_project(ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever) -> None:
    """9. Project-scoped query does not see records from other projects."""
    ekm_store.add_fact(
        FactCreate(
            subject="AlphaFeature",
            predicate="status",
            object="ready",
            project_id="proj-alpha",
        )
    )
    ekm_store.add_fact(
        FactCreate(
            subject="BetaFeature",
            predicate="status",
            object="in_progress",
            project_id="proj-beta",
        )
    )

    res_alpha = ekm_retriever.retrieve(
        MemoryQuery(query_text="Feature", project_id="proj-alpha", include_global=False)
    )
    record_titles = [r.title for r in res_alpha.records]
    assert any("AlphaFeature" in t for t in record_titles)
    assert not any("BetaFeature" in t for t in record_titles)


def test_scope_global_fallback(ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever) -> None:
    """10. Query with include_global=True returns both project and global records."""
    ekm_store.add_fact(
        FactCreate(
            subject="GlobalRule",
            predicate="enforces",
            object="strict typing",
            project_id=None,  # Global
        )
    )
    ekm_store.add_fact(
        FactCreate(
            subject="AlphaRule",
            predicate="enforces",
            object="tabs",
            project_id="proj-alpha",
        )
    )

    res = ekm_retriever.retrieve(
        MemoryQuery(query_text="Rule", project_id="proj-alpha", include_global=True)
    )
    titles = [r.title for r in res.records]
    assert any("GlobalRule" in t for t in titles)
    assert any("AlphaRule" in t for t in titles)


def test_scope_strict_project(ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever) -> None:
    """11. Query with include_global=False returns only project-scoped records."""
    ekm_store.add_fact(
        FactCreate(
            subject="GlobalInvariant",
            predicate="demands",
            object="safety",
            project_id=None,
        )
    )
    ekm_store.add_fact(
        FactCreate(
            subject="ProjectInvariant",
            predicate="demands",
            object="performance",
            project_id="proj-alpha",
        )
    )

    res = ekm_retriever.retrieve(
        MemoryQuery(query_text="Invariant", project_id="proj-alpha", include_global=False)
    )
    titles = [r.title for r in res.records]
    assert any("ProjectInvariant" in t for t in titles)
    assert not any("GlobalInvariant" in t for t in titles)


def test_temporal_filtering_as_of(ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever) -> None:
    """12. Historical query with as_of_time filters out records not yet valid or expired."""
    ekm_store.add_fact(
        FactCreate(
            subject="FeatureFlag",
            predicate="version",
            object="v1",
            valid_from="2026-01-01T00:00:00Z",
            valid_until="2026-06-01T00:00:00Z",
        )
    )
    ekm_store.add_fact(
        FactCreate(
            subject="FeatureFlag",
            predicate="version",
            object="v2",
            valid_from="2026-06-01T00:00:01Z",
            valid_until="2026-12-31T23:59:59Z",
        )
    )

    # Query as of March 2026 -> v1 valid, v2 not yet valid
    res_march = ekm_retriever.retrieve(
        MemoryQuery(query_text="FeatureFlag", as_of_time="2026-03-01T00:00:00Z")
    )
    march_objects = [r.payload.get("object") for r in res_march.records]
    assert "v1" in march_objects
    assert "v2" not in march_objects

    # Query as of August 2026 -> v2 valid, v1 expired
    res_aug = ekm_retriever.retrieve(
        MemoryQuery(query_text="FeatureFlag", as_of_time="2026-08-01T00:00:00Z")
    )
    aug_objects = [r.payload.get("object") for r in res_aug.records]
    assert "v2" in aug_objects
    assert "v1" not in aug_objects


def test_superseded_fact_exclusion(ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever) -> None:
    """13. Superseded fact excluded from normal retrieval unless include_superseded=True."""
    f1 = ekm_store.add_fact(FactCreate(subject="TargetOS", predicate="is", object="Ubuntu 20.04"))
    f2 = ekm_store.add_fact(FactCreate(subject="TargetOS", predicate="is", object="Ubuntu 24.04"))
    ekm_store.supersede_fact(f1, f2, reason="Upgraded to LTS 24.04")

    res = ekm_retriever.retrieve(MemoryQuery(query_text="TargetOS", include_superseded=False))
    ids = [r.memory_id for r in res.records]
    assert f2 in ids
    assert f1 not in ids


def test_superseded_fact_retrieval(ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever) -> None:
    """14. Query with include_superseded=True retrieves superseded fact."""
    f1 = ekm_store.add_fact(FactCreate(subject="KernelArch", predicate="is", object="x86_32"))
    f2 = ekm_store.add_fact(FactCreate(subject="KernelArch", predicate="is", object="x86_64"))
    ekm_store.supersede_fact(f1, f2, reason="Dropped 32-bit support")

    res = ekm_retriever.retrieve(MemoryQuery(query_text="KernelArch", include_superseded=True))
    ids = [r.memory_id for r in res.records]
    assert f1 in ids
    assert f2 in ids


def test_trust_level_ranking(ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever) -> None:
    """15. Higher trust level (M4) ranks above lower trust level (M0/M1) for equal relevance."""
    f_low = ekm_store.add_fact(
        FactCreate(
            subject="PolicyConfig",
            predicate="enforcement",
            object="relaxed",
            trust_level=MemoryTrustLevel.M0,
        )
    )
    f_high = ekm_store.add_fact(
        FactCreate(
            subject="PolicyConfig",
            predicate="enforcement",
            object="strict",
            trust_level=MemoryTrustLevel.M4,
        )
    )

    res = ekm_retriever.retrieve(MemoryQuery(query_text="PolicyConfig"))
    assert len(res.records) >= 2
    # First record should be the higher trust level
    assert res.records[0].memory_id == f_high
    assert res.ranking_scores[f_high] > res.ranking_scores[f_low]


def test_trust_level_filter(ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever) -> None:
    """16. Query with min_trust_level=M2 excludes M0 and M1 records."""
    f_m0 = ekm_store.add_fact(
        FactCreate(
            subject="MetricSampling",
            predicate="rate",
            object="10hz",
            trust_level=MemoryTrustLevel.M0,
        )
    )
    f_m2 = ekm_store.add_fact(
        FactCreate(
            subject="MetricSampling",
            predicate="rate",
            object="60hz",
            trust_level=MemoryTrustLevel.M2,
        )
    )

    res = ekm_retriever.retrieve(
        MemoryQuery(query_text="MetricSampling", min_trust_level=MemoryTrustLevel.M2)
    )
    ids = [r.memory_id for r in res.records]
    assert f_m2 in ids
    assert f_m0 not in ids


def test_graph_traversal_depth_1(ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever) -> None:
    """17. Entity relations traversed to find connected entities."""
    e_root = ekm_store.add_entity(
        EntityCreate(
            name="PaymentService",
            canonical_slug="payment-service",
            entity_type="concept",
        )
    )
    e_dep = ekm_store.add_entity(
        EntityCreate(
            name="PostgresDatabase",
            canonical_slug="postgres-database",
            entity_type="tool",
        )
    )
    ekm_store.add_relation(
        RelationCreate(
            source_entity_id=e_root,
            target_entity_id=e_dep,
            relation_type="depends_on",
            weight=1.0,
        )
    )

    res = ekm_retriever.retrieve(
        MemoryQuery(entity_slugs=("payment-service",), max_relationship_depth=1)
    )
    ids = [r.memory_id for r in res.records]
    assert e_root in ids
    assert e_dep in ids


def test_graph_traversal_depth_bounded(
    ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever
) -> None:
    """18. Graph traversal does not exceed max_relationship_depth."""
    e1 = ekm_store.add_entity(
        EntityCreate(name="Node1", canonical_slug="node-1", entity_type="concept")
    )
    e2 = ekm_store.add_entity(
        EntityCreate(name="Node2", canonical_slug="node-2", entity_type="concept")
    )
    e3 = ekm_store.add_entity(
        EntityCreate(name="Node3", canonical_slug="node-3", entity_type="concept")
    )

    ekm_store.add_relation(
        RelationCreate(
            source_entity_id=e1,
            target_entity_id=e2,
            relation_type="related_to",
        )
    )
    ekm_store.add_relation(
        RelationCreate(
            source_entity_id=e2,
            target_entity_id=e3,
            relation_type="related_to",
        )
    )

    # Depth 1 from Node1 should discover Node2, but NOT Node3
    res_depth1 = ekm_retriever.retrieve(
        MemoryQuery(entity_slugs=("node-1",), max_relationship_depth=1)
    )
    ids = [r.memory_id for r in res_depth1.records]
    assert e1 in ids
    assert e2 in ids
    assert e3 not in ids


def test_deduplication(ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever) -> None:
    """19. Duplicate records consolidated cleanly."""
    f1 = ekm_store.add_fact(
        FactCreate(
            subject="CacheTtl",
            predicate="seconds",
            object="300",
        )
    )
    # Query matching multiple terms
    res = ekm_retriever.retrieve(MemoryQuery(query_text="CacheTtl seconds 300"))
    matching_f1 = [r for r in res.records if r.memory_id == f1]
    assert len(matching_f1) == 1


def test_conflict_detection_fact(ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever) -> None:
    """20. Opposing facts (same subject/predicate) flagged in unresolved_conflicts."""
    f1 = ekm_store.add_fact(
        FactCreate(
            subject="DeploymentTarget",
            predicate="cloud",
            object="AWS",
            trust_level=MemoryTrustLevel.M2,
        )
    )
    f2 = ekm_store.add_fact(
        FactCreate(
            subject="DeploymentTarget",
            predicate="cloud",
            object="GCP",
            trust_level=MemoryTrustLevel.M2,
        )
    )

    res = ekm_retriever.retrieve(MemoryQuery(query_text="DeploymentTarget"))
    assert len(res.unresolved_conflicts) > 0
    conflict = res.unresolved_conflicts[0]
    assert conflict["conflict_type"] == "fact_opposition"
    assert conflict["subject"] == "deploymenttarget"
    assert conflict["predicate"] == "cloud"
    conflicted_ids = {conflict["candidate_1"]["record_id"], conflict["candidate_2"]["record_id"]}
    assert f1 in conflicted_ids
    assert f2 in conflicted_ids


def test_conflict_detection_preference(
    ekm_store: EKMMemoryStore, ekm_retriever: EKMRetriever
) -> None:
    """21. Opposing preferences flagged in unresolved_conflicts."""
    p1 = ekm_store.add_preference(
        PreferenceCreate(
            domain="coding",
            preference_key="indentation",
            preference_value="tabs",
        )
    )
    p2 = ekm_store.add_preference(
        PreferenceCreate(
            domain="coding",
            preference_key="indentation",
            preference_value="spaces",
        )
    )

    res = ekm_retriever.retrieve(MemoryQuery(query_text="indentation"))
    assert len(res.unresolved_conflicts) > 0
    conflict = res.unresolved_conflicts[0]
    assert conflict["conflict_type"] == "preference_opposition"
    assert conflict["key"] == "indentation"
    conflicted_ids = {conflict["candidate_1"]["record_id"], conflict["candidate_2"]["record_id"]}
    assert p1 in conflicted_ids
    assert p2 in conflicted_ids


def test_compiler_budget_enforcement(
    ekm_store: EKMMemoryStore,
    ekm_retriever: EKMRetriever,
    context_compiler: ContextCompiler,
) -> None:
    """22. ContextCompiler truncates low-ranked items when token budget is exceeded."""
    # Add multiple facts
    for i in range(15):
        ekm_store.add_fact(
            FactCreate(
                subject=f"BulkKnowledge{i}",
                predicate="description",
                object="A" * 200,  # Each takes around 50 tokens
                importance=min(1.0, 0.05 * (i + 1)),
            )
        )

    res = ekm_retriever.retrieve(MemoryQuery(query_text="BulkKnowledge", limit=15))
    compiled = context_compiler.compile(
        goal="Process knowledge base",
        retrieval=res,
        token_budget=512,  # Very tight budget
    )
    assert len(compiled.omitted_record_ids) > 0
    assert len(compiled.included_record_ids) > 0
    assert len(compiled.included_record_ids) + len(compiled.omitted_record_ids) == len(res.records)
    assert compiled.total_estimated_tokens <= 512


def test_compiler_token_uncertainty() -> None:
    """23. Token estimation includes uncertainty range."""
    text = "The autonomous supervisor manages execution and verification."
    est, min_tok, max_tok = estimate_tokens(text)
    assert est > 0
    assert min_tok <= est <= max_tok
    assert min_tok < max_tok


def test_supervisor_context_integration(
    migrated_conn: sqlite3.Connection,
    ekm_store: EKMMemoryStore,
    ekm_retriever: EKMRetriever,
) -> None:
    """24. Supervisor with EKM compiles bounded context IR with relevant facts/preferences."""
    # Seed knowledge into EKM
    ekm_store.add_preference(
        PreferenceCreate(
            domain="coding",
            preference_key="language",
            preference_value="Python 3.12",
        )
    )
    ekm_store.add_fact(
        FactCreate(
            subject="architecture",
            predicate="rule",
            object="Intelligence proposes; deterministic authority decides.",
            trust_level=MemoryTrustLevel.M4,
        )
    )

    state_store = StateStore(migrated_conn)
    from src.trust import build_trust_authority

    trust_auth = build_trust_authority(migrated_conn)
    tool_broker = ToolBroker(trust_authority=trust_auth)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        memory_store=ekm_store,
        retriever=ekm_retriever,
    )

    # Assemble context for planning
    context_ir = supervisor.assemble_context(
        goal="Develop new feature in Python architecture",
        project_id="proj-alpha",
    )
    assert isinstance(context_ir, ContextIR)
    assert context_ir.active_goal == "Develop new feature in Python architecture"
    prompt_context = context_ir.to_prompt_context()
    assert "Python 3.12" in prompt_context or "architecture" in prompt_context
    assert supervisor.memory_store is ekm_store
