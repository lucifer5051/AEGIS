-- AEGIS Domain Schema Foundation - EKM / Memory / Projects
-- Migration ID: 0002_ekm_schema
-- Version: 2
-- Name: ekm_schema
-- Destructive: false
--
-- Implements the first AEGIS domain schema layer.
-- Scope: projects, memory_evidence, memory_facts, user_preferences,
--        entities, entity_relationships — and their indexes.
--
-- Dependency Order (within this file):
--   projects → memory_evidence → memory_facts → user_preferences
--   projects → entities → entity_relationships
--
-- Canonical Reference: docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
-- Architecture Invariants:
--   - project_id NULL = Global/User scope (intentional; survives project deletion)
--   - project_id NOT NULL = Project scope (cascades on project deletion)
--   - Trust levels frozen at M0-M4 (defined in §4.1)
--   - Historical records preserved via supersession chain (never destroyed)
--   - No FTS5, no vector, no embeddings in this migration
--   - No credentials, secrets, or API keys in any field
--   - All timestamps: UTC ISO-8601 text (YYYY-MM-DDTHH:MM:SS.sssZ)
--   - Primary keys: UUIDv4 TEXT


-- ============================================================
-- DOMAIN 1: PROJECTS
-- ============================================================
-- Projects are the primary scope boundary for conversations,
-- memories, entities, workflows, tasks, and artifacts.
-- Global user memories (project_id IS NULL) are explicitly
-- NOT subject to project lifecycle.

CREATE TABLE IF NOT EXISTS projects (
    project_id       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    slug             TEXT NOT NULL UNIQUE,
    description      TEXT NULL,
    status           TEXT NOT NULL DEFAULT 'ACTIVE'
                     CHECK (status IN ('ACTIVE', 'PAUSED', 'COMPLETED', 'ARCHIVED', 'DELETED')),
    context_rules_json TEXT NULL,
    settings_json    TEXT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    archived_at      TEXT NULL,
    deleted_at       TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_status
    ON projects (status);

CREATE INDEX IF NOT EXISTS idx_projects_created_at
    ON projects (created_at);

CREATE INDEX IF NOT EXISTS idx_projects_slug
    ON projects (slug);


-- ============================================================
-- DOMAIN 2: MEMORY EVIDENCE
-- ============================================================
-- Evidence subsystem prevents memory hallucinations from
-- acting as unverified authorities. Evidence records must
-- exist before memory facts can reference them.
--
-- ON DELETE CASCADE: evidence tied to a project is removed
-- when the project is purged. Global evidence (project_id IS NULL)
-- is preserved.

CREATE TABLE IF NOT EXISTS memory_evidence (
    evidence_id          TEXT PRIMARY KEY,
    project_id           TEXT NULL,
    evidence_type        TEXT NOT NULL
                         CHECK (evidence_type IN (
                             'conversation_turn',
                             'user_confirmed',
                             'tool_output',
                             'imported_document',
                             'system_observation',
                             'external_web'
                         )),
    source_reference     TEXT NOT NULL,
    raw_content_snippet  TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    is_tainted           INTEGER NOT NULL DEFAULT 0
                         CHECK (is_tainted IN (0, 1)),
    trust_score          REAL NOT NULL DEFAULT 0.5
                         CHECK (trust_score >= 0.0 AND trust_score <= 1.0),
    recorded_at          TEXT NOT NULL,
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_evidence_memory_id
    ON memory_evidence (evidence_id);

CREATE INDEX IF NOT EXISTS idx_memory_evidence_project_id
    ON memory_evidence (project_id);

CREATE INDEX IF NOT EXISTS idx_memory_evidence_type
    ON memory_evidence (evidence_type);

CREATE INDEX IF NOT EXISTS idx_memory_evidence_recorded_at
    ON memory_evidence (recorded_at);


-- ============================================================
-- DOMAIN 3: MEMORY FACTS
-- ============================================================
-- Normalized atomic propositions, domain facts, and system
-- state observations. Canonical Second Brain relational memory.
--
-- Trust levels (M0-M4) are frozen architecture invariants.
-- Supersession chain preserves historical records; records are
-- never destroyed during ordinary fact updates.
-- project_id NULL = Global user knowledge scope.
-- project_id NOT NULL = Project-scoped; cascades on project purge.

CREATE TABLE IF NOT EXISTS memory_facts (
    fact_id           TEXT PRIMARY KEY,
    project_id        TEXT NULL,
    trust_level       TEXT NOT NULL
                      CHECK (trust_level IN ('M0', 'M1', 'M2', 'M3', 'M4')),
    category          TEXT NOT NULL
                      CHECK (category IN ('domain', 'user', 'system', 'procedure', 'preference', 'experience', 'lesson')),
    subject           TEXT NOT NULL,
    predicate         TEXT NOT NULL,
    object            TEXT NOT NULL,
    confidence        REAL NOT NULL DEFAULT 1.0
                      CHECK (confidence >= 0.0 AND confidence <= 1.0),
    importance        REAL NOT NULL DEFAULT 0.5
                      CHECK (importance >= 0.0 AND importance <= 1.0),
    provenance_source TEXT NOT NULL
                      CHECK (provenance_source IN ('conversation', 'tool', 'file', 'web', 'system', 'user')),
    evidence_id       TEXT NULL,
    is_superseded     INTEGER NOT NULL DEFAULT 0
                      CHECK (is_superseded IN (0, 1)),
    superseded_by     TEXT NULL,
    superseded_reason TEXT NULL,
    valid_from        TEXT NOT NULL,
    valid_until       TEXT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE,
    FOREIGN KEY (evidence_id)
        REFERENCES memory_evidence (evidence_id) ON DELETE SET NULL,
    FOREIGN KEY (superseded_by)
        REFERENCES memory_facts (fact_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_facts_project_id
    ON memory_facts (project_id);

CREATE INDEX IF NOT EXISTS idx_memory_facts_trust_level
    ON memory_facts (trust_level);

CREATE INDEX IF NOT EXISTS idx_memory_facts_scope
    ON memory_facts (project_id, trust_level);

CREATE INDEX IF NOT EXISTS idx_memory_facts_valid_from
    ON memory_facts (valid_from);

CREATE INDEX IF NOT EXISTS idx_memory_facts_valid_until
    ON memory_facts (valid_until);

CREATE INDEX IF NOT EXISTS idx_memory_facts_created_at
    ON memory_facts (created_at);

CREATE INDEX IF NOT EXISTS idx_memory_facts_is_superseded
    ON memory_facts (is_superseded);

CREATE INDEX IF NOT EXISTS idx_memory_facts_evidence_id
    ON memory_facts (evidence_id);


-- ============================================================
-- DOMAIN 4: USER PREFERENCES
-- ============================================================
-- User preferences follow the same trust/supersession model
-- as memory_facts to enable stable KV-cache hits via
-- deterministic system prompt compilation.

CREATE TABLE IF NOT EXISTS user_preferences (
    preference_id       TEXT PRIMARY KEY,
    project_id          TEXT NULL,
    domain              TEXT NOT NULL
                        CHECK (domain IN ('coding', 'communication', 'review', 'general', 'workflow', 'privacy')),
    preference_key      TEXT NOT NULL,
    preference_value    TEXT NOT NULL,
    preference_type     TEXT NOT NULL
                        CHECK (preference_type IN ('string', 'boolean', 'number', 'json')),
    trust_level         TEXT NOT NULL
                        CHECK (trust_level IN ('M0', 'M1', 'M2', 'M3', 'M4')),
    is_superseded       INTEGER NOT NULL DEFAULT 0
                        CHECK (is_superseded IN (0, 1)),
    superseded_by       TEXT NULL,
    source_evidence_id  TEXT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE,
    FOREIGN KEY (superseded_by)
        REFERENCES user_preferences (preference_id) ON DELETE SET NULL,
    FOREIGN KEY (source_evidence_id)
        REFERENCES memory_evidence (evidence_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_user_preferences_project_id
    ON user_preferences (project_id);

CREATE INDEX IF NOT EXISTS idx_user_preferences_domain
    ON user_preferences (domain);

CREATE INDEX IF NOT EXISTS idx_user_preferences_key
    ON user_preferences (preference_key);

CREATE INDEX IF NOT EXISTS idx_user_preferences_superseded
    ON user_preferences (is_superseded);


-- ============================================================
-- DOMAIN 5: ENTITIES (Second Brain)
-- ============================================================
-- Named entities in the AEGIS knowledge graph. Entities are
-- the nodes in the relational knowledge graph. Global entities
-- (project_id IS NULL) survive project purges.

CREATE TABLE IF NOT EXISTS entities (
    entity_id       TEXT PRIMARY KEY,
    project_id      TEXT NULL,
    entity_type     TEXT NOT NULL
                    CHECK (entity_type IN (
                        'person', 'organization', 'project', 'concept',
                        'file', 'task', 'workflow', 'decision', 'tool'
                    )),
    name            TEXT NOT NULL,
    canonical_slug  TEXT NOT NULL,
    description     TEXT NULL,
    metadata_json   TEXT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_entities_project_id
    ON entities (project_id);

CREATE INDEX IF NOT EXISTS idx_entities_type
    ON entities (entity_type);

CREATE INDEX IF NOT EXISTS idx_entities_slug
    ON entities (canonical_slug);


-- ============================================================
-- DOMAIN 6: ENTITY RELATIONSHIPS (Second Brain edges)
-- ============================================================
-- Directed edges in the relational knowledge graph.
-- Source and target must both exist in entities.
-- Edges inherit project scope from their source entity context.
-- Trust level reflects confidence in the relationship.

CREATE TABLE IF NOT EXISTS entity_relationships (
    relation_id       TEXT PRIMARY KEY,
    project_id        TEXT NULL,
    source_entity_id  TEXT NOT NULL,
    target_entity_id  TEXT NOT NULL,
    relation_type     TEXT NOT NULL
                      CHECK (relation_type IN (
                          'depends_on', 'author_of', 'references',
                          'owns', 'subconcept_of', 'related_to',
                          'mentions', 'contradicts', 'supports'
                      )),
    weight            REAL NOT NULL DEFAULT 1.0
                      CHECK (weight >= 0.0),
    trust_level       TEXT NOT NULL
                      CHECK (trust_level IN ('M0', 'M1', 'M2', 'M3', 'M4')),
    valid_from        TEXT NOT NULL,
    valid_until       TEXT NULL,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE,
    FOREIGN KEY (source_entity_id)
        REFERENCES entities (entity_id) ON DELETE CASCADE,
    FOREIGN KEY (target_entity_id)
        REFERENCES entities (entity_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_entity_relationships_source
    ON entity_relationships (source_entity_id);

CREATE INDEX IF NOT EXISTS idx_entity_relationships_target
    ON entity_relationships (target_entity_id);

CREATE INDEX IF NOT EXISTS idx_entity_relationships_project_id
    ON entity_relationships (project_id);

CREATE INDEX IF NOT EXISTS idx_entity_relationships_type
    ON entity_relationships (relation_type);
