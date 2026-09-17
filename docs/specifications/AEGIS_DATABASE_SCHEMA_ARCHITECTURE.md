# AEGIS Database & Schema Architecture Specification

**Document Version**: 1.0.0-SCHEMA-SPEC  
**Status**: ARCHITECTURAL SPECIFICATION ONLY — NOT IMPLEMENTED  
**Date**: 2026-09-17  
**Scope**: Canonical Relational Data Model, Second-Brain EKM, Temporal State, Audit, and Storage Lifecycle for AEGIS  
**Canonical Repository**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. Relational Persistence Foundation

### 1.1 Canonical Persistence Engine
AEGIS adopts **SQLite** (version 3.40+ compatible) as its singular, canonical local relational persistence engine.
* **No Database Server Dependencies**: AEGIS does **not** depend on, require, or integrate external database servers (such as PostgreSQL, MySQL, MongoDB, Redis, Neo4j, or Qdrant) for its core authoritative persistence.
* **Host Service Independence**: The host machine's existing running background services (e.g., MySQL 8.0 on port 3306, MongoDB on port 27017) are external third-party environments and are explicitly **not** AEGIS dependencies.
* **Zero Configuration Deployment**: SQLite runs embedded in-process, requiring zero external server configuration, zero network listener binding, and zero port contention on Windows.

### 1.2 Storage Separation & OneDrive Operational Constraint
The canonical repository is hosted within Microsoft OneDrive at `C:\Users\digvi\OneDrive\Desktop\project\aeigs`. The database architecture explicitly partitions file types to isolate repository source code from active database I/O:

| Layer | Path / Classification | Characteristics & OneDrive Handling |
| :--- | :--- | :--- |
| **A. Repository Source & Schemas** | `C:\Users\digvi\OneDrive\Desktop\project\aeigs\` | Declarative markdown, JSON schemas, documentation, tools. Safely synchronized by OneDrive. |
| **B. Authoritative Database File** | `data/aegis_authoritative.db` | Primary SQLite relational file. ACID transactions, immutable event logs, authoritative user records. |
| **C. SQLite Journal / WAL Files** | `data/aegis_authoritative.db-wal`, `*-shm` | Write-Ahead Log. High-frequency random I/O. **Operational Risk**: OneDrive synchronization can conflict with Windows shared-memory file locks (`EPERM` / `file locked`). |
| **D. Derived & Rebuildable Indexes** | `runtime/indexes/*.vec`, `*.fts` | Secondary FTS5 and vector index files. Rebuildable deterministically from Authoritative DB. |
| **E. Ephemeral & Transient Caches** | `runtime/cache/`, `runtime/scratch/` | Model scratchpads, temporary inference buffers, telemetry chunks. Excluded from backup and git. |

> **Operational Implementation Constraint**:  
> To mitigate OneDrive sync locks on SQLite WAL and temporary cache files, the architecture establishes that while the repository remains canonical in OneDrive, runtime configuration may later map active runtime files (`*-wal`, `*.tmp`, temporary vector caches) to local non-synced temporary storage (e.g., `%LOCALAPPDATA%\AEGIS\runtime` or excluded cache paths) via storage configuration parameters. This decision is architectural; physical runtime placement will be finalized during runtime configuration prompts.

---

## 2. Authoritative vs. Derived Data Architecture

To ensure total disaster recovery resilience, AEGIS establishes five mutually exclusive data classes:

```
+------------------------------------------------------------------------------------+
|                                    AEGIS DATA                                      |
+------------------------------------------------------------------------------------+
  │
  ├── [1] AUTHORITATIVE (Single Source of Truth, User-owned, Fully Backed Up)
  │     └── Facts, Preferences, Decisions, Projects, Workflows, Tasks, Artifacts, Events
  │
  ├── [2] OPERATIONAL_STATE (Execution Management, Restart-Resilient, Selectively Backed Up)
  │     └── Active Task State, Workflow Runs, Schedules, Autonomy Policy, Hardware Profile
  │
  ├── [3] DERIVED_REBUILDABLE (Disposables, Generated from Class 1, Never Backed Up)
  │     └── Vector Embeddings, FTS5 Inverted Indexes, Graph Projections, Compiled Contexts
  │
  ├── [4] SECRET_EXCLUDED (Cryptographic Secrets, Never in DB, Never in Context)
  │     └── API Tokens, OAuth Refresh Keys, Encryption Keys, System Passwords
  │
  ├── [5] TRANSIENT_EXCLUDED (Volatile Execution State, Cleared on Restart, Discarded)
        └── In-flight Turn Tokens, KV Cache, Temporary Tool Stdout, Memory Scratchpads
```

1. **Authoritative Principle**: All knowledge, memories, workflows, decisions, and evidence can be 100% recovered without embeddings, vector stores, FTS indexes, or AI model weights.
2. **Derived Invariant**: Derived indexes (FTS5, vector tables, graph caches) must **never** become the sole storage location of any fact, preference, or user interaction.

---

## 3. Schema Conventions & Standards

* **Primary Keys**: Strongly typed UUIDv4 strings (`TEXT PRIMARY KEY`) or millisecond-monotonic prefixed ULIDs (`TEXT PRIMARY KEY`) to prevent primary key collision across distributed or multi-channel synchronization.
* **Timestamps**: All timestamps are stored as **UTC ISO-8601 text strings** (`YYYY-MM-DDTHH:MM:SS.SSSZ`) or integer epoch milliseconds (`INTEGER`). Canonical standard: ISO-8601 UTC text format for auditability.
* **Foreign Keys**: Explicit SQLite foreign keys enforced via `PRAGMA foreign_keys = ON;`.
  * Cascading deletes (`ON DELETE CASCADE`) are strictly applied to project-owned and entity-dependent rows.
  * Restrictive deletes (`ON DELETE RESTRICT`) are applied to security-critical definitions (e.g., preventing deletion of an active Skill while assigned to a running Workflow).
* **Nullability**: Explicit `NOT NULL` constraints on all structural invariants, identifiers, and timestamps.
* **JSON Usage Rules**:
  * **Permitted**: Dynamic tool parameter dictionaries, context IR payloads, extensible metadata bags.
  * **Prohibited**: JSON must **never** be used to circumvent relational normalization for entities, foreign keys, relationships, tasks, or event logs.
* **Soft vs. Hard Deletion**:
  * Soft deletion (`is_deleted = 1`, `deleted_at`) is used for user recovery of tasks, workflows, and individual memories.
  * Hard deletion (`DELETE FROM ... WHERE project_id = ...`) is mandatory for project cascade purges and right-to-be-forgotten compliance.

---

## 4. Machine-First Epistemic Knowledge Model (EKM)

AEGIS rejects human-centric unstructured Markdown notes as authoritative memory. Canonical knowledge is persisted as machine-first relational records.

### 4.1 Frozen Memory Trust Levels (M0 to M4)
Every persisted memory item carries a deterministic, immutable trust score:
* **M0 (Raw Observation)**: Unfiltered observation from environment, sensor, untrusted user transcript, or raw tool stdout.
* **M1 (Agent Claim)**: Hypothesis or deduction generated by an internal cognitive model; unverified by external tool or user.
* **M2 (Tool-Supported)**: Output corroborated by deterministic tool execution, filesystem state, or verifiable system API return.
* **M3 (Verified)**: Corroborated across multiple independent deterministic tools or verified against authoritative documentation.
* **M4 (User-Confirmed)**: Explicitly affirmed, dictated, or corrected by the authenticated user.

*External and web-scraped content is initially ingested strictly at level M0 (Tainted / Untrusted).*

### 4.2 Canonical Memory Entities
* **`memory_facts`**: Normalized atomic propositions, domain facts, and system state observations.
* **`memory_preferences`**: User preferences (communication styles, operational bounds, workflow preferences).
* **`memory_decisions`**: Explicit choices made by the user or agent, containing rationale, considered alternatives, and constraints.
* **`memory_experiences`**: Post-mortem reflections on completed tasks, including failure modes and successful patterns.
* **`memory_lessons`**: Abstracted operational guidelines derived from multiple experiences.
* **`memory_procedures`**: Repeatable execution instructions, tool call sequences, and problem-solving patterns.
* **`memory_candidates`**: Staged memory proposals awaiting background consolidation, deduplication, or user confirmation.

### 4.3 Provenance, Scope, and Supersession Schema
```sql
-- Conceptual Schema: Memory Facts
CREATE TABLE memory_facts (
    fact_id TEXT PRIMARY KEY,
    project_id TEXT NULL,                       -- NULL = Global Scope; UUID = Project Scope
    trust_level TEXT NOT NULL CHECK (trust_level IN ('M0', 'M1', 'M2', 'M3', 'M4')),
    category TEXT NOT NULL,                     -- 'domain', 'user', 'system', 'procedure'
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    importance REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0.0 AND importance <= 1.0),
    provenance_source TEXT NOT NULL,            -- 'conversation', 'tool', 'file', 'web'
    evidence_id TEXT NULL,
    is_superseded INTEGER NOT NULL DEFAULT 0 CHECK (is_superseded IN (0, 1)),
    superseded_by TEXT NULL,
    superseded_reason TEXT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NULL,                      -- NULL = currently valid indefinitely
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY (superseded_by) REFERENCES memory_facts(fact_id) ON DELETE SET NULL,
    FOREIGN KEY (evidence_id) REFERENCES memory_evidence(evidence_id) ON DELETE SET NULL
);
```

---

## 5. Temporal Memory & Conflict Resolution

* **Temporal Bounding**: Every memory record contains `valid_from` and `valid_until` ISO-8601 timestamps.
* **Supersession Chain**: When a preference or fact changes (e.g., user changes preferred output language or Python version), the previous record is marked `is_superseded = 1`, `superseded_by = :new_id`, and `valid_until = :now`.
* **Historical Preservation**: Historical records are **never destroyed** during ordinary preference or fact updates. Historical queries can reconstruct the exact cognitive state of AEGIS at any past point in time (`AS OF :timestamp`).
* **Conflict Resolution Strategy**:
  1. Trust Level Precedence: Higher trust level always takes precedence ($M4 > M3 > M2 > M1 > M0$).
  2. Temporal Recency: Among items of identical trust level, the more recent `valid_from` timestamp takes precedence.
  3. Scope Precedence: Explicit `project_id` scope overrides Global scope for project-specific operations.

---

## 6. Memory Evidence Subsystem

The evidence subsystem prevents memory hallucinations from acting as unverified authorities.

```sql
-- Conceptual Schema: Memory Evidence
CREATE TABLE memory_evidence (
    evidence_id TEXT PRIMARY KEY,
    project_id TEXT NULL,
    evidence_type TEXT NOT NULL CHECK (evidence_type IN (
        'conversation_turn', 'user_confirmed', 'tool_output', 
        'imported_document', 'system_observation', 'external_web'
    )),
    source_reference TEXT NOT NULL,             -- message_id, tool_call_id, file_path, URL
    raw_content_snippet TEXT NOT NULL,          -- Sanitized verbatim evidence snippet
    content_hash TEXT NOT NULL,                 -- SHA-256 hash of original source
    is_tainted INTEGER NOT NULL DEFAULT 0 CHECK (is_tainted IN (0, 1)), -- External content = 1
    trust_score REAL NOT NULL DEFAULT 0.5,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
```

* **Trust Core Invariant**: Memory evidence stores verifiable provenance, but possessing evidence never grants execution rights. All actions proposed based on evidence must still traverse Trust Core authorization.

---

## 7. Second Brain Relational Knowledge Graph

AEGIS models Second Brain relationships using **normalized relational structures**, eliminating the operational complexity of external graph database daemons.

```sql
-- Conceptual Schema: Second Brain Entities
CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY,
    project_id TEXT NULL,                       -- NULL = Global Entity
    entity_type TEXT NOT NULL CHECK (entity_type IN (
        'person', 'organization', 'project', 'concept', 
        'file', 'task', 'workflow', 'decision', 'tool'
    )),
    name TEXT NOT NULL,
    canonical_slug TEXT NOT NULL,
    description TEXT NULL,
    metadata_json TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

-- Conceptual Schema: Second Brain Relationships
CREATE TABLE entity_relationships (
    relation_id TEXT PRIMARY KEY,
    project_id TEXT NULL,
    source_entity_id TEXT NOT NULL,
    target_entity_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,                 -- 'depends_on', 'author_of', 'references', 'owns', 'subconcept_of'
    weight REAL NOT NULL DEFAULT 1.0,
    trust_level TEXT NOT NULL CHECK (trust_level IN ('M0', 'M1', 'M2', 'M3', 'M4')),
    valid_from TEXT NOT NULL,
    valid_until TEXT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY (source_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE,
    FOREIGN KEY (target_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
);
```

---

## 8. Adaptive Personalization & User Preferences

To support KV-cache hit efficiency, AEGIS decouples stable user persona rules from dynamic project context.

```sql
-- Conceptual Schema: Personalization Preferences
CREATE TABLE user_preferences (
    preference_id TEXT PRIMARY KEY,
    project_id TEXT NULL,                       -- NULL = Global; UUID = Project Specific
    domain TEXT NOT NULL,                       -- 'coding', 'communication', 'review', 'general'
    preference_key TEXT NOT NULL,
    preference_value TEXT NOT NULL,
    preference_type TEXT NOT NULL CHECK (preference_type IN ('string', 'boolean', 'number', 'json')),
    trust_level TEXT NOT NULL CHECK (trust_level IN ('M0', 'M1', 'M2', 'M3', 'M4')),
    is_superseded INTEGER NOT NULL DEFAULT 0 CHECK (is_superseded IN (0, 1)),
    superseded_by TEXT NULL,
    source_evidence_id TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY (superseded_by) REFERENCES user_preferences(preference_id) ON DELETE SET NULL,
    FOREIGN KEY (source_evidence_id) REFERENCES memory_evidence(evidence_id) ON DELETE SET NULL
);
```

* **Compilation Rule**: The personal system prompt is compiled hierarchically: `Base Invariants (Frozen)` + `Global Stable Preferences` + `Domain Preferences` + `Project Dynamic Context`. System prompt text is never physically modified on disk per turn.

---

## 9. Projects Architecture

Projects are first-class organizational entities.

```sql
-- Conceptual Schema: Projects
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'PAUSED', 'COMPLETED', 'ARCHIVED', 'DELETED')),
    context_rules_json TEXT NULL,               -- Project-specific constraints and scope
    settings_json TEXT NULL,                    -- Scoped tool permissions and budgets
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT NULL,
    deleted_at TEXT NULL
);
```

---

## 10. Channel-Affine Conversations & Sessions

AEGIS preserves channel affinity across Desktop and WhatsApp without automatic cross-channel message mirroring.

```sql
-- Conceptual Schema: Conversations
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    project_id TEXT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('desktop', 'whatsapp', 'system_cli')),
    channel_conversation_id TEXT NOT NULL,       -- WhatsApp JID, Desktop Window ID
    title TEXT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'IDLE', 'CLOSED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL
);

-- Conceptual Schema: Messages
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    channel TEXT NOT NULL CHECK (channel IN ('desktop', 'whatsapp', 'system_cli')),
    content TEXT NOT NULL,
    delivery_status TEXT NOT NULL CHECK (delivery_status IN ('QUEUED', 'DELIVERED', 'FAILED', 'READ')),
    raw_payload_json TEXT NULL,
    tokens_consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
);
```

---

## 11. Workflows & Tasks Execution Model

### 11.1 Workflow Definitions & Instances
```sql
CREATE TABLE workflow_definitions (
    workflow_id TEXT PRIMARY KEY,
    project_id TEXT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    dag_specification_json TEXT NOT NULL,       -- Strongly typed DAG schema
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE TABLE workflow_runs (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    project_id TEXT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED')),
    triggered_by TEXT NOT NULL,                 -- 'user', 'schedule', 'event'
    started_at TEXT NOT NULL,
    completed_at TEXT NULL,
    telemetry_json TEXT NULL,
    FOREIGN KEY (workflow_id) REFERENCES workflow_definitions(workflow_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
```

### 11.2 DAG-Style Tasks
```sql
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    parent_task_id TEXT NULL,
    workflow_run_id TEXT NULL,
    project_id TEXT NULL,
    title TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED', 'BLOCKED')),
    dependency_task_ids_json TEXT NULL,         -- JSON array of task_id prerequisites
    contract_request_json TEXT NOT NULL,        -- Strongly typed Task Contract
    contract_result_json TEXT NULL,             -- Strongly typed Task Result
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    started_at TEXT NULL,
    completed_at TEXT NULL,
    FOREIGN KEY (parent_task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
    FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
```

---

## 12. Durable Task Scheduling Subsystem

```sql
CREATE TABLE schedules (
    schedule_id TEXT PRIMARY KEY,
    project_id TEXT NULL,
    workflow_id TEXT NULL,
    target_action TEXT NOT NULL,
    schedule_type TEXT NOT NULL CHECK (schedule_type IN ('ONE_SHOT', 'CRON_RECURRING', 'EVENT_TRIGGERED')),
    cron_expression TEXT NULL,
    trigger_condition_json TEXT NULL,
    next_run_at TEXT NOT NULL,
    last_run_at TEXT NULL,
    missed_run_policy TEXT NOT NULL CHECK (missed_run_policy IN ('RUN_ONCE', 'SKIP', 'FAIL_ALERT')),
    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY (workflow_id) REFERENCES workflow_definitions(workflow_id) ON DELETE CASCADE
);
```

---

## 13. Immutable Event & History Architecture

Events form an append-only transaction ledger. Normal operations never edit historical event rows.

```sql
CREATE TABLE event_log (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,                   -- e.g. 'TASK_COMPLETED', 'APPROVAL_REQUESTED'
    subject_type TEXT NOT NULL,                 -- 'task', 'workflow', 'project', 'memory'
    subject_id TEXT NOT NULL,
    project_id TEXT NULL,                       -- Enables complete cascade purge on project deletion
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'manager', 'skill', 'trust_core', 'system')),
    actor_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,               -- Traces causal multi-step execution chains
    causation_id TEXT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL,                 -- Strictly sanitized: NO SECRETS OR PASSWORDS
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
```

---

## 14. Skill Registry & Model Abstraction

The user interacts exclusively with skills and capabilities; internal model architectures are abstracted.

```sql
CREATE TABLE skill_registry (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    installation_state TEXT NOT NULL CHECK (installation_state IN ('PROVISIONED', 'STAGED', 'UNAVAILABLE', 'FAILED')),
    health_status TEXT NOT NULL CHECK (health_status IN ('HEALTHY', 'DEGRADED', 'UNHEALTHY', 'UNKNOWN')),
    requested_capabilities_json TEXT NOT NULL,  -- Required tools and system access
    implementation_metadata_json TEXT NULL,     -- Abstracted internal execution config
    installed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

---

## 15. Operational Autonomy Tiers & Approvals

Operational autonomy tiers (0–3) are maintained separately from deterministic action-risk classifications.

```sql
CREATE TABLE system_autonomy_policy (
    policy_id TEXT PRIMARY KEY,
    project_id TEXT NULL,                       -- NULL = System Global Autonomy
    autonomy_tier INTEGER NOT NULL CHECK (autonomy_tier IN (0, 1, 2, 3)),
    -- Tier 0: Strict Manual Approval
    -- Tier 1: Safe / Bounded Execution
    -- Tier 2: Supervised Autonomy
    -- Tier 3: Full Bounded Task Autonomy
    configured_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE TABLE approval_requests (
    request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    project_id TEXT NULL,
    action_name TEXT NOT NULL,
    action_risk_level TEXT NOT NULL CHECK (action_risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    target_resource TEXT NOT NULL,
    parameters_summary TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'REVOKED')),
    decision_actor TEXT NULL,
    decision_at TEXT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
```

* **Trust Core Authority Rule**: The presence of an approval row in the database does not authorize execution. The Trust Core validates the signature, state, and expiration deterministically before issuing a short-lived execution ticket.

---

## 16. Security & Audit Logging Subsystem

Auditing records security-critical events. **Zero-Secret Invariant**: Passwords, OAuth tokens, API secrets, and encryption keys are strictly prohibited from audit storage.

```sql
CREATE TABLE audit_records (
    audit_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'AUTHZ_DECISION', 'CAPABILITY_ISSUED', 'CAPABILITY_REVOKED', 
        'APPROVAL_GRANTED', 'APPROVAL_DENIED', 'POLICY_CHANGE', 
        'DESTRUCTIVE_OP', 'PROJECT_PURGE', 'BACKUP_CREATED', 
        'RESTORE_EXECUTED', 'CREDENTIAL_ACCESSED', 'NETWORK_BLOCKED'
    )),
    actor TEXT NOT NULL,
    target_entity_type TEXT NOT NULL,
    target_entity_id TEXT NOT NULL,
    project_id TEXT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('ALLOW', 'DENY', 'ESCALATE')),
    policy_rule_id TEXT NOT NULL,
    telemetry_json TEXT NOT NULL,               -- Context without secrets (e.g. SHA-256 of key, not key)
    timestamp TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
```

---

## 17. Artifacts & File Metadata Subsystem

Files and artifacts are identified by **logical identifiers and content hashes**, decoupling identity from Windows absolute paths.

```sql
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    project_id TEXT NULL,
    logical_name TEXT NOT NULL,
    artifact_type TEXT NOT NULL CHECK (artifact_type IN ('CODE', 'DOCUMENT', 'DATASET', 'REPORT', 'IMAGE', 'BINARY')),
    sha256_hash TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    storage_relative_path TEXT NOT NULL,        -- Relative to workspace or data directory
    mime_type TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('ACTIVE', 'ARCHIVED', 'DELETED')),
    created_at TEXT NOT NULL,
    deleted_at TEXT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
```

---

## 18. Priority-Based Notification Subsystem

Notifications support priority bands P0 to P4 with channel-affine delivery and quiet-hour deferral:

```sql
CREATE TABLE notifications (
    notification_id TEXT PRIMARY KEY,
    priority TEXT NOT NULL CHECK (priority IN ('P0', 'P1', 'P2', 'P3', 'P4')),
    -- P0 = Emergency Halt / Security Alert (Immediate all channels)
    -- P1 = User Approval Required (Immediate active channel)
    -- P2 = Task Failure / Attention Required
    -- P3 = Workflow Completion
    -- P4 = Digest / Informational / Background Indexing
    target_channel TEXT NOT NULL CHECK (target_channel IN ('desktop', 'whatsapp', 'system_cli')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    approval_request_id TEXT NULL,
    delivery_state TEXT NOT NULL CHECK (delivery_state IN ('PENDING', 'SENT', 'FAILED', 'SUPPRESSED')),
    is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
    created_at TEXT NOT NULL,
    delivered_at TEXT NULL,
    FOREIGN KEY (approval_request_id) REFERENCES approval_requests(request_id) ON DELETE SET NULL
);
```

---

## 19. Backup Metadata Subsystem

Backups exclusively capture Authoritative User Content and Operational State. Encryption keys are excluded from the database.

```sql
CREATE TABLE backup_sets (
    backup_id TEXT PRIMARY KEY,
    backup_type TEXT NOT NULL CHECK (backup_type IN ('LOCAL_SNAPSHOT', 'CLOUD_ARCHIVE')),
    cloud_provider TEXT NULL,                   -- e.g. 'google_drive'
    backup_state TEXT NOT NULL CHECK (backup_state IN ('IN_PROGRESS', 'COMPLETED', 'FAILED', 'VERIFIED')),
    is_incremental INTEGER NOT NULL DEFAULT 0 CHECK (is_incremental IN (0, 1)),
    parent_backup_id TEXT NULL,
    archive_sha256 TEXT NOT NULL,
    total_bytes INTEGER NOT NULL,
    manifest_summary_json TEXT NOT NULL,        -- Inventory of projects, workflows, docs included
    created_at TEXT NOT NULL,
    verified_at TEXT NULL,
    FOREIGN KEY (parent_backup_id) REFERENCES backup_sets(backup_id) ON DELETE RESTRICT
);
```

---

## 20. Hardware Profile & Provisioning State

```sql
CREATE TABLE hardware_profile (
    profile_id TEXT PRIMARY KEY,
    benchmark_version TEXT NOT NULL,
    cpu_model TEXT NOT NULL,
    cpu_cores INTEGER NOT NULL,
    ram_total_bytes INTEGER NOT NULL,
    gpu_name TEXT NOT NULL,
    vram_total_bytes INTEGER NOT NULL,
    driver_version TEXT NOT NULL,
    cuda_compute_capability TEXT NULL,
    storage_read_iops REAL NOT NULL,
    storage_write_iops REAL NOT NULL,
    is_valid INTEGER NOT NULL DEFAULT 1 CHECK (is_valid IN (0, 1)),
    invalidation_reason TEXT NULL,
    benchmarked_at TEXT NOT NULL
);
```

---

## 21. Deletion, Retention, and Project Cascade Purge

### 21.1 Cascade Purge Semantics
When a project is permanently deleted (`DELETE FROM projects WHERE project_id = :id`), the following cascade sequence executes within an atomic transaction:
1. **Authoritative Purge**: Foreign key `ON DELETE CASCADE` automatically deletes all rows in `tasks`, `workflow_runs`, `workflow_definitions`, `schedules`, `conversations`, `messages`, `memory_facts`, `memory_preferences`, `memory_decisions`, `entities`, `entity_relationships`, and `event_log` associated with `project_id`.
2. **Artifact Purge**: All project-owned files matching `artifacts.project_id` are unlinked from disk.
3. **Derived Index Purge**: Project-scoped vector embeddings in secondary vector tables and FTS5 tokens matching `project_id` are deleted.
4. **Global Memory Preservation Invariant**: Global memories (`project_id IS NULL`) are **never deleted**. Cross-project relationships where the deleted project was the target entity are unlinked (`ON DELETE CASCADE` on relation edges only; source entities in global scope remain untouched).

---

## 22. Indexing Strategy: Relational vs. Derived

| Index Type | Engine / Implementation | Target Fields | Storage / Rebuildability |
| :--- | :--- | :--- | :--- |
| **B-Tree (Relational)** | Native SQLite Indexes | `project_id`, `created_at`, `valid_from`, `valid_until`, `state`, `trust_level`, `correlation_id` | Authoritative DB file. Fast exact-match queries. |
| **Full-Text (FTS5)** | SQLite FTS5 Virtual Tables | `messages.content`, `memory_facts.object`, `artifacts.logical_name` | Derived file / auxiliary tables. 100% rebuildable from authoritative tables. |
| **Vector Index** | Embedded Vector Store (`sqlite-vec` / binary index) | `memory_facts`, `entities`, `artifacts` text embeddings | Derived file. 100% rebuildable by re-embedding authoritative records. |
| **Graph Adjacency** | In-memory cached adjacency list | `entity_relationships` table edges | Derived cache. Rebuilt on demand from relational tables. |

---

## 23. Database Migration Strategy

1. **Versioned Migration Sequencing**: Migrations follow sequential numbering (`001_initial_schema.sql`, `002_add_ekm_tables.sql`).
2. **Migration Tracking Table**:
   ```sql
   CREATE TABLE schema_migrations (
       migration_id INTEGER PRIMARY KEY,
       version_name TEXT NOT NULL UNIQUE,
       applied_at TEXT NOT NULL,
       checksum_sha256 TEXT NOT NULL,
       execution_duration_ms INTEGER NOT NULL
   );
   ```
3. **Transactional Execution**: Every migration script executes inside an explicit transaction (`BEGIN TRANSACTION; ... COMMIT;`). Any DDL error triggers an automatic rollback.
4. **Pre-Migration Safety Rule**: Destructive migrations require an automated pre-migration SQLite backup snapshot before applying schema alterations.

---
*End of Database & Schema Architecture Specification.*
