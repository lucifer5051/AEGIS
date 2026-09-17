# AEGIS Data Classification Matrix

**Document Version**: 1.0.0-SCHEMA-SPEC  
**Status**: ARCHITECTURAL SPECIFICATION ONLY — NOT IMPLEMENTED  
**Date**: 2026-09-17  
**Scope**: Canonical Data Classification, Backup Eligibility, Deletion Behavior, Rebuildability, and Security Categorization across all AEGIS Domains  
**Canonical Repository**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. Classification Categories Overview

AEGIS categorizes all system, user, and agent information into five mutually exclusive architectural classes:

| Class | Definition | Backup Policy | Retention / Deletion Policy | Rebuildability |
| :--- | :--- | :--- | :--- | :--- |
| **`USER_CONTENT`** | Core user-authored or user-owned work, documents, structured memories, projects, workflows, and task outputs. | **MANDATORY INCLUDED** in Local and Cloud backups. | Retained indefinitely until explicit user deletion or project cascade purge. | **Authoritative**. Irreplaceable if deleted without backup. |
| **`OPERATIONAL_STATE`** | System execution management, scheduler triggers, autonomy tiers, skill registrations, and hardware profiles. | **SELECTIVELY INCLUDED** (configuration and schedules needed to resume work). | Updated in place; history pruned according to retention window. | Semi-rebuildable via system reconfiguration or discovery. |
| **`DERIVED_REBUILDABLE`** | Secondary computational assets: vector embeddings, FTS5 indexes, compiled Context IR, graph adjacency caches. | **EXPLICITLY EXCLUDED** from all backups. | Automatically purged on project cascade; disposable during storage reclamation. | **100% Rebuildable** from Authoritative records. |
| **`SECRET_EXCLUDED`** | Cryptographic secrets: API keys, OAuth tokens, private encryption keys, operating system credentials. | **STRICTLY PROHIBITED** from ordinary database backups. | Managed exclusively by Credential Broker in dedicated secure storage. | Managed via explicit credential re-entry / external keyring. |
| **`TRANSIENT_EXCLUDED`** | Volatile in-flight inference buffers, KV caches, active turn scratchpads, raw tool stdout streams. | **EXPLICITLY EXCLUDED**. | Discarded automatically upon turn completion or system process restart. | Transient; not rebuildable or required. |

---

## 2. Comprehensive Domain Entity Matrix

| Domain / Table / Entity | Data Class | Backup Eligibility | Deletion / Cascade Behavior | Rebuildability | Security & Taint Considerations |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`projects`** | `USER_CONTENT` | **INCLUDED** | Soft-delete or Permanent Purge. Hard delete triggers cascade. | Authoritative | Project context rules may contain sensitive user domain constraints. |
| **`project_settings`** | `USER_CONTENT` | **INCLUDED** | Cascades with `project_id`. | Authoritative | Contains tool permission whitelist; must be integrity-verified. |
| **`memory_facts`** | `USER_CONTENT` | **INCLUDED** | Project facts cascade; Global facts preserved. | Authoritative | Tracks M0–M4 trust levels and taint flags. Sanitized on write. |
| **`memory_preferences`** | `USER_CONTENT` | **INCLUDED** | Project preferences cascade; Global preserved. | Authoritative | High privacy sensitivity. Informs personal system prompt. |
| **`memory_decisions`** | `USER_CONTENT` | **INCLUDED** | Project decisions cascade; Global preserved. | Authoritative | Contains user and agent reasoning rationale; audit-traceable. |
| **`memory_experiences`** | `USER_CONTENT` | **INCLUDED** | Project experiences cascade; Global preserved. | Authoritative | Post-mortem reflections. Excludes raw sensitive tokens. |
| **`memory_lessons`** | `USER_CONTENT` | **INCLUDED** | Retained globally unless tagged to project. | Authoritative | General heuristic knowledge extracted from experiences. |
| **`memory_procedures`** | `USER_CONTENT` | **INCLUDED** | Retained globally unless tagged to project. | Authoritative | Tool call procedures and repeatable workflows. |
| **`memory_evidence`** | `USER_CONTENT` | **INCLUDED** | Project evidence cascades with project deletion. | Authoritative | Stores verbatim snippets; marked tainted if from external web/untrusted sources. |
| **`entities`** (Second Brain) | `USER_CONTENT` | **INCLUDED** | Project entities cascade; Global entities preserved. | Authoritative | Knowledge graph nodes (people, concepts, orgs). |
| **`entity_relationships`** | `USER_CONTENT` | **INCLUDED** | Cascades if source or target project entity deleted. | Authoritative | Relational graph edges with confidence and trust ratings. |
| **`conversations`** | `USER_CONTENT` | **INCLUDED** | Project-associated conversations cascade. | Authoritative | Channel-affine sessions (Desktop, WhatsApp). |
| **`messages`** | `USER_CONTENT` | **INCLUDED** | Cascades with `conversation_id`. | Authoritative | Sanitized conversation turns. Raw secrets must never be present. |
| **`workflow_definitions`** | `USER_CONTENT` | **INCLUDED** | Project workflows cascade; Global templates preserved. | Authoritative | Versioned DAG execution templates. |
| **`workflow_runs`** | `USER_CONTENT` | **INCLUDED** | Cascades with `project_id`. | Authoritative | Execution history required to restore operational user work. |
| **`tasks`** | `USER_CONTENT` | **INCLUDED** | Cascades with `project_id` or `workflow_run_id`. | Authoritative | Task contracts, status, and task results. |
| **`artifacts`** | `USER_CONTENT` | **INCLUDED** | Metadata in DB; file payload backed up with project. | Authoritative | Identified by SHA-256 hash and logical name. |
| **`schedules`** | `OPERATIONAL_STATE`| **INCLUDED** | Project schedules cascade; System schedules kept. | Authoritative | Durable cron and timer triggers for task dispatch. |
| **`event_log`** | `OPERATIONAL_STATE`| **INCLUDED** | Project events cascade; System events archived. | Authoritative | Append-only immutable log. Strictly sanitized; no raw secrets. |
| **`skill_registry`** | `OPERATIONAL_STATE`| **INCLUDED** | Preserved across project lifecycles. | Semi-rebuildable | Records enabled capabilities and requested permission grants. |
| **`system_autonomy_policy`** | `OPERATIONAL_STATE`| **INCLUDED** | Project overrides cascade; Global policy kept. | Authoritative | Operational autonomy tier (0–3). Trust Core enforcement. |
| **`approval_requests`** | `OPERATIONAL_STATE`| **INCLUDED** | Cascades with `project_id`. | Authoritative | Immutable records of user approvals/rejections. |
| **`audit_records`** | `OPERATIONAL_STATE`| **INCLUDED** | Preserved for compliance; purged only via security policy. | Authoritative | Tamper-resistant security log. Excludes credentials. |
| **`notifications`** | `OPERATIONAL_STATE`| **SELECTIVE** | Cleared after retention window (e.g. 30 days). | Operational | P0–P4 priority delivery states across channels. |
| **`backup_sets`** | `OPERATIONAL_STATE`| **INCLUDED** | Preserved to verify backup chain integrity. | Authoritative | Manifest hashes and inventory of backup archives. |
| **`hardware_profile`** | `OPERATIONAL_STATE`| **EXCLUDED** | Invalidated on hardware/driver change. | Rebuildable | Benchmark scores and detected hardware topology. |
| **`vector_embeddings`** | `DERIVED_REBUILDABLE`| **EXCLUDED** | Purged automatically on project deletion. | 100% Rebuildable | Generated via embedding model from authoritative facts. |
| **`fts5_inverted_index`** | `DERIVED_REBUILDABLE`| **EXCLUDED** | Purged automatically on project deletion. | 100% Rebuildable | Generated via SQLite FTS5 tokenizer from text fields. |
| **`graph_adjacency_cache`**| `DERIVED_REBUILDABLE`| **EXCLUDED** | Flushed and rebuilt dynamically in memory. | 100% Rebuildable | Generated from `entity_relationships` table. |
| **`compiled_context_ir`** | `DERIVED_REBUILDABLE`| **EXCLUDED** | Ephemeral per reasoning cycle. | 100% Rebuildable | Compiled by Context Engine prior to model invocation. |
| **`api_tokens_and_keys`** | `SECRET_EXCLUDED` | **EXCLUDED** | Managed in OS-protected credential store. | External | Never stored in SQLite or passed in LLM context. |
| **`oauth_refresh_tokens`** | `SECRET_EXCLUDED` | **EXCLUDED** | Managed in OS-protected credential store. | External | Securely isolated; injected only at network transport layer. |
| **`in_flight_kv_cache`** | `TRANSIENT_EXCLUDED`| **EXCLUDED** | Cleared immediately when process terminates. | Transient | VRAM/RAM model inference state. |
| **`tool_scratch_buffers`** | `TRANSIENT_EXCLUDED`| **EXCLUDED** | Flushed after tool subprocess terminates. | Transient | Raw execution stdout/stderr scratch files. |

---

## 3. Disaster Recovery & Rebuildability Invariants

1. **Complete Offline Reconstruction**: A newly initialized AEGIS installation can ingest an authoritative backup archive and reconstruct all vector embeddings, FTS5 search indexes, and graph adjacencies using background workers with zero manual intervention.
2. **Deterministic Purge Compliance**: A project cascade deletion removes all `USER_CONTENT`, `OPERATIONAL_STATE`, and `DERIVED_REBUILDABLE` records bound to `project_id`, leaving zero orphaned derived embeddings or lingering search tokens in secondary stores.
3. **Secret Sanitization Guarantee**: No entity classified as `SECRET_EXCLUDED` is ever serialized into backup archives, event payloads, or audit records.

---
*End of Data Classification Matrix.*
