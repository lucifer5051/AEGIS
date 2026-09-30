# SAGE Schema Architecture Manifest

**Manifest Version**: 1.0.0-FROZEN-SCHEMA-SPEC  
**Generated Date**: 2026-09-17  
**Phase**: DATABASE_AND_SCHEMA_ARCHITECTURE  
**Prompt**: 004  
**Canonical Project Root**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. Relational Persistence Target
* **Database Engine**: Embedded SQLite 3.40+ (Standalone, zero-server architecture).
* **Third-Party Server Independence**: Host services (MongoDB, MySQL) are explicitly **not** SAGE dependencies.
* **Mandatory PRAGMA Settings**:
  * `PRAGMA foreign_keys = ON;` (Strict referential integrity enforcement on every connection).
  * `PRAGMA journal_mode = WAL;` (Write-Ahead Logging for high concurrency and crash resilience).
  * `PRAGMA synchronous = NORMAL;` (Optimal durability/performance balance in WAL mode).
  * `PRAGMA busy_timeout = 5000;` (Graceful lock contention wait on Windows filesystem).
* **OneDrive Runtime Constraint**:
  * Active SQLite WAL files (`*-wal`, `*-shm`) and ephemeral runtime scratchpads risk Windows shared-memory file-locking conflicts with Microsoft OneDrive synchronization.
  * Architectural strategy: Source code, schemas, and migrations reside in the canonical OneDrive repository. Runtime configuration options will coordinate placement of high-frequency temporary WAL and derived cache files to local non-sync directories in future implementation phases.

---

## 2. Inventory of Defined Relational Entities

The relational schema architecture models 25 canonical entities across 4 operational domains:

### Domain A: Epistemic Knowledge Model (Second Brain)
1. `memory_facts`: Atomic domain propositions, verified state observations, and truths.
2. `memory_preferences`: User communication, behavioral, and technical preferences.
3. `memory_decisions`: Explicit architectural, task, and personal choices with rationale.
4. `memory_experiences`: Post-task evaluations and operational reflections.
5. `memory_lessons`: General heuristics abstracted from multiple experiences.
6. `memory_procedures`: Repeatable recipes, tool execution sequences, and workflows.
7. `memory_evidence`: Cryptographic source excerpts, hashes, and taint classifications.
8. `memory_candidates`: Staged memory proposals awaiting consolidation.
9. `entities`: Second Brain knowledge graph vertices (people, concepts, tools, organizations).
10. `entity_relationships`: Second Brain knowledge graph edges with confidence and trust metrics.

### Domain B: Executive Task & Workflow Cluster
11. `projects`: First-class organizational containers with context rules and lifecycle state.
12. `project_settings`: Scoped tool whitelists, budgets, and security permissions.
13. `workflow_definitions`: Versioned procedural templates and DAG specifications.
14. `workflow_runs`: Concrete workflow execution instances with state tracking.
15. `tasks`: Discrete execution units with parent-child hierarchy and DAG dependency arrays.
16. `schedules`: Durable cron and timer triggers resilient to host reboots.
17. `event_log`: Append-only, immutable transaction ledger.
18. `artifacts`: Logical file representations identified by SHA-256 hash and relative paths.

### Domain C: Channels & Interaction Cluster
19. `conversations`: Channel-affine dialog sessions (Desktop, WhatsApp, CLI).
20. `messages`: Individual conversational turns with delivery status and token metrics.
21. `notifications`: Priority-based alerts (P0 to P4) with channel routing rules.

### Domain D: Security, Audit & Platform Operations
22. `system_autonomy_policy`: Operational autonomy tiers (0 to 3) per project and system.
23. `approval_requests`: Deterministic authorization requests for sensitive actions.
24. `audit_records`: Tamper-resistant, zero-secret security audit log.
25. `skill_registry`: Capability registry with permission requirements (abstracting models).
26. `hardware_profile`: Host compute benchmarks and hardware profile validity state.
27. `backup_sets`: Manifest records of local and cloud backup archives.
28. `schema_migrations`: Versioned ledger of applied database migrations.

---

## 3. Core Architectural Enums & Standards

### 3.1 Memory Trust Levels (Frozen)
* **`M0`**: Raw observation (sensors, user speech transcript, tainted external web data).
* **`M1`**: Agent claim (internal neural hypothesis; uncorroborated).
* **`M2`**: Tool-supported (corroborated by deterministic tool or OS execution).
* **`M3`**: Verified (cross-corroborated across independent tools or authoritative docs).
* **`M4`**: User-confirmed (explicitly confirmed or dictated by the user).

### 3.2 Operational Autonomy Tiers (Frozen)
* **`Tier 0`**: Strict Manual Approval (every tool, network, or write action requires confirmation).
* **`Tier 1`**: Safe / Bounded Execution (read-only and local sandboxes autonomous; mutations gated).
* **`Tier 2`**: Supervised Autonomy (pre-approved routine project operations autonomous; destructive gated).
* **`Tier 3`**: Full Bounded Task Autonomy (autonomous execution within scoped workflow budgets).

### 3.3 Notification Priorities (Frozen)
* **`P0`**: Emergency Halt / Security Alert (Immediate all channels; bypasses quiet hours).
* **`P1`**: User Approval Required (Immediate active channel).
* **`P2`**: Task Failure / Attention Required (Standard channel alert).
* **`P3`**: Workflow Completion (Informational channel update).
* **`P4`**: Digest / Background Telemetry (Batch queued; respects quiet hours).

---

## 4. Implementation Boundary & Next Prerequisites

* **Executable Code Status**: Zero application code, zero virtual environments, zero packages, zero SQLite database files, and zero SQL migration scripts exist.
* **Architecture Compliance**: All 28 entities adhere strictly to the frozen technical foundation specification (`AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md`).
* **Prerequisites for Prompt 005**:
  1. Provision isolated Python 3.12 virtual environment (`.venv`) inside the canonical workspace.
  2. Install foundational lightweight typing and validation packages (`pydantic`).
  3. Validate Python 3.12 SQLite driver and WAL mode operational capabilities.

---
*End of Schema Architecture Manifest.*
