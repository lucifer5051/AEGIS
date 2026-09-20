# AEGIS Domain Schema Foundation Specification

**Document Version**: 1.0.0-DOMAIN-SCHEMA  
**Status**: IMPLEMENTED  
**Date**: 2026-09-20  
**Scope**: First AEGIS Domain Schema Layer — EKM/Memory, Projects, Conversations  
**Canonical Repository**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  
**Implemented In**: Migrations 0002, 0003  

---

## 1. Purpose

This document specifies the first AEGIS domain schema layer implemented over the
SQLite Storage Foundation (Prompt 011). It covers the relational structure for:

- **Projects** — first-class organizational scope boundaries
- **EKM / Memory** — machine-first epistemic knowledge (facts, evidence, preferences, entities, relationships)
- **Conversations & Messages** — channel-affine interaction records

No business logic is implemented. This layer provides the relational schema, constraints,
indexes, and FK relationships only.

---

## 2. Dependency Order

The migration files must be applied in strictly increasing version order. Table
creation within each migration respects FK dependency ordering:

```
Migration 0001: storage_metadata, (schema_migrations bootstrapped by runner)

Migration 0002 (ekm_schema):
  projects                    ← no FK deps (root anchor)
  ↓
  memory_evidence             ← FK: projects
  ↓
  memory_facts                ← FK: projects, memory_evidence (self-reference: superseded_by)
  ↓
  user_preferences            ← FK: projects, memory_evidence (self-reference: superseded_by)
  ↓
  entities                    ← FK: projects
  ↓
  entity_relationships        ← FK: projects, entities (source + target)

Migration 0003 (conversations_schema):
  conversations               ← FK: projects (ON DELETE SET NULL)
  ↓
  messages                    ← FK: conversations (ON DELETE CASCADE)
```

---

## 3. Projects

### 3.1 Table: `projects`

The primary scope boundary for AEGIS data. All project-owned records reference
`project_id` via foreign keys.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `project_id` | TEXT | PRIMARY KEY | UUIDv4 identifier |
| `name` | TEXT | NOT NULL | Display name |
| `slug` | TEXT | NOT NULL UNIQUE | URL-safe unique identifier |
| `description` | TEXT | NULL | Optional description |
| `status` | TEXT | NOT NULL, CHECK | `ACTIVE\|PAUSED\|COMPLETED\|ARCHIVED\|DELETED` |
| `context_rules_json` | TEXT | NULL | Project-specific scope constraints |
| `settings_json` | TEXT | NULL | Scoped tool permissions/budgets |
| `created_at` | TEXT | NOT NULL | UTC ISO-8601 |
| `updated_at` | TEXT | NOT NULL | UTC ISO-8601 |
| `archived_at` | TEXT | NULL | UTC ISO-8601 when archived |
| `deleted_at` | TEXT | NULL | UTC ISO-8601 when soft-deleted |

### 3.2 Project Scope vs. Global Scope

Records with `project_id IS NULL` are **global/user-scope** and survive
project deletion. Records with `project_id = :uuid` are **project-scoped**
and cascade on project purge.

```
project_id IS NULL  →  Global user knowledge (preserved always)
project_id = UUID   →  Project scope (ON DELETE CASCADE)
```

### 3.3 Project Lifecycle

```
ACTIVE → PAUSED → ACTIVE
ACTIVE → COMPLETED → ARCHIVED
ACTIVE → DELETED (triggers cascade purge of project-owned records)
```

---

## 4. EKM / Memory

### 4.1 Frozen Trust Levels (M0–M4)

Trust levels are architectural invariants. No new levels may be added without a
frozen architecture change.

| Level | Name | Description |
|-------|------|-------------|
| M0 | Raw Observation | Unfiltered observation; untrusted source |
| M1 | Agent Claim | Hypothesis/deduction from internal model |
| M2 | Tool-Supported | Corroborated by deterministic tool execution |
| M3 | Verified | Corroborated across multiple independent tools |
| M4 | User-Confirmed | Explicitly affirmed/corrected by authenticated user |

External/web-scraped content enters at **M0** and is flagged `is_tainted = 1` in evidence.

### 4.2 Table: `memory_evidence`

Evidence prevents hallucinations from acting as unverified authorities.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `evidence_id` | TEXT | PRIMARY KEY | UUIDv4 |
| `project_id` | TEXT | NULL | FK → projects (CASCADE) |
| `evidence_type` | TEXT | NOT NULL, CHECK | See §4.2.1 |
| `source_reference` | TEXT | NOT NULL | message_id, tool_call_id, URL, file path |
| `raw_content_snippet` | TEXT | NOT NULL | Sanitized verbatim evidence snippet |
| `content_hash` | TEXT | NOT NULL | SHA-256 of original source content |
| `is_tainted` | INTEGER | NOT NULL, CHECK | 1 = external/untrusted content |
| `trust_score` | REAL | NOT NULL, CHECK [0,1] | Numeric confidence (0.0–1.0) |
| `recorded_at` | TEXT | NOT NULL | UTC ISO-8601 |

#### 4.2.1 Evidence Types
```
conversation_turn   — Evidence from a specific conversation turn
user_confirmed      — User explicitly confirmed a fact
tool_output         — Tool/command execution result
imported_document   — Content from an imported file or document
system_observation  — System-level state observation
external_web        — Web-scraped content (always is_tainted=1 initially)
```

### 4.3 Table: `memory_facts`

Normalized atomic propositions stored as subject-predicate-object triples.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `fact_id` | TEXT | PRIMARY KEY | UUIDv4 |
| `project_id` | TEXT | NULL | FK → projects (CASCADE) |
| `trust_level` | TEXT | NOT NULL, CHECK | M0–M4 |
| `category` | TEXT | NOT NULL, CHECK | `domain\|user\|system\|procedure\|preference\|experience\|lesson` |
| `subject` | TEXT | NOT NULL | Entity the fact is about |
| `predicate` | TEXT | NOT NULL | Relationship/property |
| `object` | TEXT | NOT NULL | Value/target of the fact |
| `confidence` | REAL | NOT NULL, CHECK [0,1] | Confidence score (default 1.0) |
| `importance` | REAL | NOT NULL, CHECK [0,1] | Importance weight (default 0.5) |
| `provenance_source` | TEXT | NOT NULL, CHECK | `conversation\|tool\|file\|web\|system\|user` |
| `evidence_id` | TEXT | NULL | FK → memory_evidence (SET NULL) |
| `is_superseded` | INTEGER | NOT NULL, CHECK | 0=current, 1=historical |
| `superseded_by` | TEXT | NULL | FK → memory_facts (self-ref, SET NULL) |
| `superseded_reason` | TEXT | NULL | Human-readable supersession rationale |
| `valid_from` | TEXT | NOT NULL | UTC ISO-8601 validity start |
| `valid_until` | TEXT | NULL | UTC ISO-8601 validity end (NULL = indefinite) |
| `created_at` | TEXT | NOT NULL | UTC ISO-8601 |
| `updated_at` | TEXT | NOT NULL | UTC ISO-8601 |

**Supersession Invariant**: Historical records are never physically destroyed during
fact updates. `is_superseded = 1` marks a record as historical; `superseded_by`
points to the newer fact.

### 4.4 Table: `user_preferences`

User preferences follow the same trust/supersession model for deterministic
system prompt compilation (stable KV-cache hits).

Key constraints mirror `memory_facts`: project FK, supersession self-reference,
evidence FK, trust level M0–M4.

Supported domains: `coding | communication | review | general | workflow | privacy`

### 4.5 Table: `entities`

Named entities (nodes) in the AEGIS Second Brain knowledge graph.

Entity types: `person | organization | project | concept | file | task | workflow | decision | tool`

### 4.6 Table: `entity_relationships`

Directed edges in the relational knowledge graph between entities.

Relation types: `depends_on | author_of | references | owns | subconcept_of | related_to | mentions | contradicts | supports`

Each edge carries: `weight`, `trust_level`, `valid_from`, `valid_until` for temporal graph queries.

---

## 5. Conversations & Messages

### 5.1 Channel Affinity Invariant

AEGIS preserves channel affinity. Responses return exclusively to the originating
channel. Cross-channel message mirroring is prohibited.

Supported channels: `desktop | whatsapp | system_cli`

### 5.2 Table: `conversations`

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `conversation_id` | TEXT | PRIMARY KEY | UUIDv4 |
| `project_id` | TEXT | NULL | FK → projects **(ON DELETE SET NULL)** |
| `channel` | TEXT | NOT NULL, CHECK | `desktop\|whatsapp\|system_cli` |
| `channel_conversation_id` | TEXT | NOT NULL | WhatsApp JID / Desktop window ID |
| `title` | TEXT | NULL | Optional display title |
| `status` | TEXT | NOT NULL, CHECK | `ACTIVE\|IDLE\|CLOSED` |
| `created_at` | TEXT | NOT NULL | UTC ISO-8601 |
| `updated_at` | TEXT | NOT NULL | UTC ISO-8601 |

> **Why ON DELETE SET NULL?**  
> Conversation history is user-owned authoritative data. When a project is deleted,
> conversation records should not be purged — they become orphaned (project_id=NULL)
> and remain queryable for user recovery, audit, and cross-project analysis.

### 5.3 Table: `messages`

Individual turns within a conversation.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `message_id` | TEXT | PRIMARY KEY | UUIDv4 |
| `conversation_id` | TEXT | NOT NULL | FK → conversations **(ON DELETE CASCADE)** |
| `role` | TEXT | NOT NULL, CHECK | `user\|assistant\|system\|tool` |
| `channel` | TEXT | NOT NULL, CHECK | Must match conversation channel |
| `message_seq` | INTEGER | NOT NULL | Sequence number within conversation |
| `content` | TEXT | NOT NULL | Message body (no credentials) |
| `delivery_status` | TEXT | NOT NULL, CHECK | `QUEUED\|DELIVERED\|FAILED\|READ` |
| `raw_payload_json` | TEXT | NULL | Channel-native payload (no secrets) |
| `tokens_consumed` | INTEGER | NOT NULL, CHECK ≥0 | LLM token usage |
| `created_at` | TEXT | NOT NULL | UTC ISO-8601 |

**UNIQUE(conversation_id, message_seq)** — Prevents duplicate ordering positions.

---

## 6. Foreign Key Behavior Summary

| Child Table | Parent Table | Column | ON DELETE |
|-------------|-------------|--------|-----------|
| `memory_evidence` | `projects` | `project_id` | CASCADE |
| `memory_facts` | `projects` | `project_id` | CASCADE |
| `memory_facts` | `memory_evidence` | `evidence_id` | SET NULL |
| `memory_facts` | `memory_facts` | `superseded_by` | SET NULL |
| `user_preferences` | `projects` | `project_id` | CASCADE |
| `user_preferences` | `memory_evidence` | `source_evidence_id` | SET NULL |
| `user_preferences` | `user_preferences` | `superseded_by` | SET NULL |
| `entities` | `projects` | `project_id` | CASCADE |
| `entity_relationships` | `projects` | `project_id` | CASCADE |
| `entity_relationships` | `entities` | `source_entity_id` | CASCADE |
| `entity_relationships` | `entities` | `target_entity_id` | CASCADE |
| `conversations` | `projects` | `project_id` | **SET NULL** |
| `messages` | `conversations` | `conversation_id` | CASCADE |

---

## 7. Indexing Strategy

Only relational B-tree indexes are defined. FTS5 and vector indexes are deferred
(derived/rebuildable; not in this prompt).

### Projects
- `idx_projects_status`
- `idx_projects_created_at`
- `idx_projects_slug`

### Memory Evidence
- `idx_memory_evidence_project_id`
- `idx_memory_evidence_type`
- `idx_memory_evidence_recorded_at`

### Memory Facts
- `idx_memory_facts_project_id`
- `idx_memory_facts_trust_level`
- `idx_memory_facts_scope` (project_id, trust_level)
- `idx_memory_facts_valid_from`
- `idx_memory_facts_valid_until`
- `idx_memory_facts_created_at`
- `idx_memory_facts_is_superseded`
- `idx_memory_facts_evidence_id`

### Entities & Relationships
- `idx_entities_project_id`, `idx_entities_type`, `idx_entities_slug`
- `idx_entity_relationships_source`, `_target`, `_project_id`, `_type`

### Conversations & Messages
- `idx_conversations_project_id`, `_channel`, `_status`, `_created_at`, `_project_channel`
- `idx_messages_conversation_seq`, `_conversation_created`, `_role`, `_delivery_status`

---

## 8. Security Invariants

- No column in any domain table is intended to store passwords, API keys,
  OAuth tokens, refresh tokens, encryption keys, or credential material.
- `raw_payload_json` in `messages` must be sanitized before storage.
- `external_web` evidence must always be stored with `is_tainted = 1`.
- `context_rules_json` / `settings_json` in projects must not contain credentials.

---

## 9. Data Classification

| Domain | Class | Backup Required |
|--------|-------|-----------------|
| projects | AUTHORITATIVE | Yes |
| memory_facts | AUTHORITATIVE | Yes |
| memory_evidence | AUTHORITATIVE | Yes |
| user_preferences | AUTHORITATIVE | Yes |
| entities | AUTHORITATIVE | Yes |
| entity_relationships | AUTHORITATIVE | Yes |
| conversations | AUTHORITATIVE | Yes |
| messages | AUTHORITATIVE | Yes |

All domain tables in this prompt are Class 1 AUTHORITATIVE data. They must be
included in backup sets and are never treated as rebuildable derived data.

---

## 10. What is NOT Implemented

The following are explicitly deferred to future prompts:

- FTS5 full-text search indexes
- Vector/embedding indexes
- EKM retrieval engine
- Memory consolidation / deduplication
- Business logic for project lifecycle management
- Workflow, task, scheduler tables
- Audit, event log, notifications
- Artifact metadata
- Skill registry
- Trust Core / approval subsystem
- Model provisioning
- WhatsApp transport
- Cloud backup subsystem

---

## 11. Tests

`tests/test_domain_schema.py` — 41 tests covering:

- All migrations apply (version 1→3)
- Migration ordering and idempotency
- Foreign key enforcement
- All domain tables present; no unexpected tables
- Projects: insert, duplicate ID/slug rejection, status validation
- Memory facts: global/project scope, M0–M4 validation, temporal fields,
  supersession chain, confidence range, invalid FK rejection
- Memory evidence: relationship, invalid type rejection, tainted evidence
- Entities/relationships: insert, invalid type/FK rejection
- Conversations: insert, channel validation (desktop/whatsapp/system_cli), global scope
- Messages: insert, ordering uniqueness, role validation, FK rejection
- Project cascade: project-owned facts removed, global facts survive
- Integrity check: `PRAGMA integrity_check` passes
- Health check: `DatabaseHealthState.HEALTHY`
- Security: no secret-named columns in any domain table
- Full lifecycle smoke test

---

*End of AEGIS Domain Schema Foundation Specification.*
