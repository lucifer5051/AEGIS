# AEGIS Data Relationships & Structural Architecture

**Document Version**: 1.0.0-SCHEMA-SPEC  
**Status**: ARCHITECTURAL SPECIFICATION ONLY — NOT IMPLEMENTED  
**Date**: 2026-09-17  
**Scope**: Relational Mapping, Entity Graph Topologies, Cascade Deletion Boundaries, and Data Flow Pathways  
**Canonical Repository**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. High-Level Entity-Relationship Topology

The AEGIS relational data architecture is structured into four tightly coordinated operational clusters:
1. **The Executive Execution Cluster**: Projects, Workflows, Tasks, and Append-Only Event Logs.
2. **The Epistemic Memory Cluster (Second Brain)**: Facts, Preferences, Decisions, Entities, Relations, and Evidence.
3. **The Interaction & Delivery Cluster**: Channels, Conversations, Messages, and Priority Notifications.
4. **The Security & Governance Cluster**: Autonomy Policies, Approval Requests, Capabilities, and Audit Trails.

```
+----------------------------------------------------------------------------------------------------+
|                                      EXECUTIVE EXECUTION CLUSTER                                   |
|                                                                                                    |
|   +--------------+  1:N   +----------------------+  1:N   +---------------+  1:N   +-----------+   |
|   |   projects   +--------> workflow_definitions +--------> workflow_runs +-------->   tasks   |   |
|   +-------+------+        +----------------------+        +---------------+        +-----+-----+   |
|           |                                                                              |         |
|           | 1:N                                                                          | 1:N     |
|           v                                                                              v         |
|   +-------+------+                                                                 +-----+-----+   |
|   |  schedules   |                                                                 | event_log |   |
|   +--------------+                                                                 +-----------+   |
+-----------+------------------------------------------------------------------------------+---------+
            |                                                                              |
            | 1:N                                                                          | 1:N
            v                                                                              v
+-----------+----------------------------------------------------+   +---------------------+---------+
|              EPISTEMIC MEMORY CLUSTER (SECOND BRAIN)           |   |   SECURITY & GOVERNANCE CLUSTER   |
|                                                                |   |                               |
|   +------------------+  1:N   +----------------------------+   |   |   +-----------------------+   |
|   |     entities     +-------->    entity_relationships    |   |   |   | system_autonomy_policy|   |
|   +------------------+        +----------------------------+   |   |   +-----------+-----------+   |
|            ^                                                   |   |               | 1:N           |
|            | References                                        |   |               v               |
|   +--------+---------+  N:1   +----------------------------+   |   |   +-----------+-----------+   |
|   |   memory_facts   +-------->      memory_evidence       |   |   |   |   approval_requests   |   |
|   +--------+---------+        +----------------------------+   |   |   +-----------+-----------+   |
|            |                                                   |   |               | 1:1           |
|            | Supersession (Unary 1:1)                          |   |               v               |
|            v                                                   |   |   +-----------+-----------+   |
|   [superseded_by_id]                                           |   |   |     audit_records     |   |
+----------------------------------------------------------------+   +-------------------------------+
```

---

## 2. Core Domain Execution Hierarchy

The hierarchy strictly enforces the principle that **Project**, **Workflow**, and **Task** represent distinct lifecycle stages:

```
Project (Root Domain Boundary)
  │
  ├── Workflow Definition (Reusable procedural template & DAG schema)
  │     │
  │     └── Workflow Run / Instance (Concrete execution instance with runtime parameters)
  │           │
  │           └── Tasks (Discrete, strongly typed executable units)
  │                 │
  │                 ├── Parent Task -> Child Task (Hierarchical subtasks)
  │                 ├── Task Prerequisite Dependencies (DAG dependency array)
  │                 ├── Task Contracts & Results (Strongly typed payloads)
  │                 └── Resulting Artifacts & Events
```

* **Relational Foreign Key Actions**:
  * Deleting a `projects` record cascades (`ON DELETE CASCADE`) to all associated `workflow_definitions`, `workflow_runs`, `tasks`, and `schedules`.
  * Deleting a `workflow_definitions` record is **restricted** (`ON DELETE RESTRICT`) if active `workflow_runs` are currently in flight.
  * Deleting a `tasks` record cascades to child tasks and removes pending task approval requests.

---

## 3. Epistemic Knowledge Model & Evidence Graph

### 3.1 Unary Supersession Chains
When facts, preferences, or decisions evolve over time, the schema maintains a complete immutable lineage:

```
[Memory Record V1] (valid_from: T0, valid_until: T1, is_superseded: 1)
        │
        └── superseded_by ──> [Memory Record V2] (valid_from: T1, valid_until: T2, is_superseded: 1)
                                      │
                                      └── superseded_by ──> [Memory Record V3] (valid_from: T2, valid_until: NULL, is_superseded: 0)
```

### 3.2 Evidence Grounding
* Every `memory_facts`, `memory_preferences`, and `memory_decisions` entry maintains an optional foreign key link to `memory_evidence`.
* `memory_evidence` preserves verbatim excerpts, cryptographic SHA-256 source hashes, and taint classification (`is_tainted = 1` for external web resources).
* Deleting a project automatically cascades to all project-scoped evidence while preserving unattached global evidence.

---

## 4. Channel, Conversation, and Notification Linkages

```
+------------------------------------------------------------------------------------+
|                         CHANNELS & CONVERSATION TOPOLOGY                           |
+------------------------------------------------------------------------------------+

  Channel: Desktop Client ──────> Conversation: Desktop Main ──────> Messages (Role: User/Assistant)
                                                                            │
                                                                            └── Mentions/Triggers Task
                                                                                      │
  Channel: WhatsApp ────────────> Conversation: WA Direct ─────────> Messages         v
                                                                            │   Approval Request
                                                                            │         │
                                                                            v         v
                                                                       Notification (Priority P0-P4)
                                                                       Routed per Explicit Policy
```

* **Channel Affinity Principle**:
  * User prompts arriving on `whatsapp` create a `messages` record bound to a `conversations` entry with `channel = 'whatsapp'`.
  * The response is emitted exclusively to the originating channel.
  * Normal conversational traffic never mirrors across channels.
  * Notifications route across channels **only** when an explicit user notification policy allows cross-channel routing for priority P0 or P1 alerts.

---

## 5. Trust Core, Approvals, and Audit Topology

```
Skill Execution Proposal
        │
        ▼
Trust Core Policy Engine (Evaluates System Autonomy Tier 0-3 vs Action Risk Level)
        │
        ├── [ALLOWED] ───────────────────────────> Direct Tool Broker Execution
        │                                                     │
        └── [REQUIRES APPROVAL]                               │
                  │                                           │
                  ▼                                           ▼
          approval_requests (Pending) ──> Emits Event ──> audit_records (Immutable)
                  │
                  ├── Approved ──> Issues Execution Ticket ──> Tool Broker Runs
                  └── Rejected ──> Aborts Execution ──> Returns Contract Error to Manager
```

* **Zero Direct Model Execution**: Models and Skills never write directly to `audit_records` or manipulate `approval_requests` state; these operations are managed exclusively by the deterministic Trust Core layer.

---

## 6. Cascade Deletion Boundary Architecture

```
+------------------------------------------------------------------------------------+
|                     PROJECT CASCADE DELETION BOUNDARY MAP                          |
+------------------------------------------------------------------------------------+

  TARGET FOR PERMANENT DELETION: project_id = "proj-123"

  [CASCADE DELETED] (ON DELETE CASCADE)
  ├── projects (Target row)
  ├── project_settings (Foreign key: project_id)
  ├── workflow_definitions (Foreign key: project_id)
  ├── workflow_runs (Foreign key: project_id)
  ├── tasks (Foreign key: project_id)
  ├── schedules (Foreign key: project_id)
  ├── conversations & messages (Where project_id = "proj-123")
  ├── memory_facts & memory_evidence (Where project_id = "proj-123")
  ├── memory_preferences & decisions (Where project_id = "proj-123")
  ├── entities & entity_relationships (Where project_id = "proj-123")
  ├── artifacts (Where project_id = "proj-123" + physical file unlinking)
  ├── event_log (Where project_id = "proj-123")
  └── derived_indexes (Secondary vector & FTS5 tokens matching project_id)

  ============================== SECURITY BARRIER ==============================

  [STRICTLY PRESERVED] (GLOBAL MEMORY PROTECTION)
  ├── memory_facts (Where project_id IS NULL)
  ├── user_preferences (Global preferences, project_id IS NULL)
  ├── entities (Global concepts, tools, organizations)
  ├── audit_records (Archived compliance trail of the deletion event itself)
  ├── backup_sets (Historical backup archives remain immutable)
  └── hardware_profile (System capability state unchanged)
```

---

## 7. Data Flow: Authoritative SQLite vs. Derived Projections

```
+------------------------------------------------------------------------------------+
|                     AUTHORITATIVE TO DERIVED DATA FLOW PIPELINE                    |
+------------------------------------------------------------------------------------+

  [INPUT / WRITE PATH]
  User Interaction / Skill Completion
          │
          ▼
  1. Authoritative SQLite Commit (ACID WAL Transaction)
     ├── memory_facts
     ├── messages
     ├── entities & relationships
     └── artifacts
          │
          ▼
  2. Asynchronous Indexing Dispatcher (Background Worker)
          │
          ├───> [Derived FTS5 Pipeline] ────> SQLite FTS5 Virtual Tables (BM25 Tokenizer)
          │
          ├───> [Derived Vector Pipeline] ──> Embedding Model ──> Vector Store (Cosine/L2)
          │
          └───> [Derived Graph Pipeline] ───> Adjacency Matrix Cache (Entity Link Hops)

  ------------------------------------------------------------------------------------

  [RETRIEVAL / READ PATH]
  User Query
          │
          ▼
  Context Engine Multi-Stage Retrieval
     ├── Stage 1: Exact Match Query ──────> FTS5 Virtual Table
     ├── Stage 2: Semantic Match Query ───> Vector Distance Search
     └── Stage 3: Relationship Hops ──────> Graph Adjacency Cache
          │
          ▼
  Candidate Union & Reranking ────────────> Resolves Authoritative IDs against SQLite
          │
          ▼
  Compiled Context IR ───────────────────> Passes Compact Token-Efficient Payload to Manager
```

---
*End of Data Relationships & Structural Architecture.*
