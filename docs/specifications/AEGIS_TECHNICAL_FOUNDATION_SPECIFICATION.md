# AEGIS Technical Foundation Specification

**Document Version**: 1.0.0-FROZEN  
**Status**: ARCHITECTURAL SPECIFICATION ONLY — NOT IMPLEMENTED  
**Date**: 2026-09-17  
**Scope**: Complete Technical Foundation for the AEGIS Autonomous Personal Digital Agent

---

## 1. AEGIS System Overview

AEGIS is an autonomous personal digital agent and cognitive assistant designed to operate as a local-first, machine-first personal "Second Brain" and executive task coordinator. AEGIS bridges high-level user intentionality with deterministic, auditable machine execution across heterogeneous host environments.

AEGIS is built upon a dual-system separation of concerns:
1. **The Intelligence Layer (Cognitive Core)**: Composed of a central Manager/Supervisor and bounded, task-oriented Skills. This layer parses intent, performs multi-step reasoning, synthesizes retrieved context, formulates plans, and proposes concrete actions.
2. **The Deterministic Authority Layer (Trust Core & Operating Substrate)**: Composed of the Trust Core, Tool Broker, Credential Broker, Memory Store, and Execution Engines. This layer deterministically authenticates, authorizes, audits, executes, and verifies every proposed operation without delegating final security decisions to neural network inference.

AEGIS operates across multiple user-facing communication channels (Desktop PC, WhatsApp, and extensible future channels) while maintaining a singular, unified state across memory, active projects, workflows, and security profiles.

---

## 2. Architectural Principles

1. **Intelligence Proposes; Deterministic Authority Decides**:
   Probabilistic neural network outputs (LLMs/VLMs) are treated as untrusted proposals. No model output is granted direct system execution privileges without traversing deterministic validation, capability scoping, and policy enforcement.
2. **Manager as Central Coordinator**:
   A single, authoritative Manager model coordinates all high-level user interactions, skill dispatch, context synthesis, and decision orchestration.
3. **Skill-Based Bounded Execution**:
   Skills are task-specific worker modules executed under strict capability boundaries. Skills communicate exclusively with the Manager via strongly typed task contracts and results; skills never act as peer-to-peer autonomous controllers.
4. **Trust Core Outside the Intelligence Layer**:
   Security boundaries, authorization checks, and audit logging exist strictly external to the LLM/VLM context window. System-prompt guardrails are never considered definitive security controls.
5. **Memory is Not Authority**:
   Retrieved memory items represent historical context, observations, and derived knowledge—not hard authorization credentials or immediate execution rights.
6. **External Content is Untrusted**:
   Any data entering AEGIS from external networks, user uploads, web scraping, messaging channels, or third-party APIs is treated as tainted and quarantined from execution control paths.
7. **Remember Broadly, Retrieve Selectively**:
   AEGIS persists rich structured facts, decisions, events, and relationships, but compiles only a minimal, highly compact Context Information Representation (Context IR) for model reasoning.
8. **Capability Selection vs. Implementation Abstraction**:
   The user selects desired functional capabilities and skills; AEGIS autonomously selects, provisions, and configures appropriate internal models, runtimes, and local algorithms based on host hardware, resource availability, compatibility, and security policy. Internal model implementation details are abstracted from the user-facing interface and are not exposed as normal user-facing concepts.
9. **Hardware-Governed Model Provisioning**:
   Local inference configurations, quantization levels, and model concurrency are determined strictly by verified hardware profiles and resource limits.
10. **Manager-Priority Lifecycle**:
    The central Manager model has absolute priority in resource allocation, memory residence, and safe multi-phase updates.
11. **Performance-First Context Efficiency**:
    Context tokens are treated as scarce computational resources. Hot-path caching, pre-filtering, and token-efficient structured compilation take precedence over brute-force context stuffing.
12. **Separation of Authoritative vs. Rebuildable Data**:
    Authoritative structured facts, logs, and artifacts are strictly decoupled from derived representations (vector embeddings, FTS5 indexes, graph traversals), which can be rebuilt deterministically from authoritative records.

---

## 3. Component Boundaries

The AEGIS architecture establishes five distinct, isolated operational boundaries:

```
+-----------------------------------------------------------------------+
|                           USER INTERFACES                             |
|  [Desktop Client]        [WhatsApp Channel]      [Future Channels]    |
+-----------------------------------+-----------------------------------+
                                    | Channel-Affine Adapter
+-----------------------------------v-----------------------------------+
|                        INTELLIGENCE LAYER                             |
|                                                                       |
|  +-----------------------------------------------------------------+  |
|  |                 CENTRAL MANAGER / SUPERVISOR                    |  |
|  +--------------------------------+--------------------------------+  |
|                                   | Task Contracts                    |
|  +--------------------------------v--------------------------------+  |
|  |                          SKILLS                                 |  |
|  |  [Workflow] [File Ops] [Web Research] [Vision] [Personalization]|  |
|  +-----------------------------------------------------------------+  |
+-----------------------------------+-----------------------------------+
                                    | Proposals & Contract Requests
+-----------------------------------v-----------------------------------+
|                     DETERMINISTIC TRUST CORE                          |
|  +-----------------------------------------------------------------+  |
|  |  Policy Engine      Capability Scoper      Audit Logger         |  |
|  |  Approval Engine    Credential Broker      Tool Broker          |  |
|  +-----------------------------------------------------------------+  |
+-----------------------------------+-----------------------------------+
                                    | Authorized Invocations
+-----------------------------------v-----------------------------------+
|                    SYSTEM & PERSISTENCE LAYER                         |
|  +-----------------------------------------------------------------+  |
|  |  Machine-First Memory Store (Events, Entities, Relations)       |  |
|  |  Context Engine (FTS5, Vector, Graph, Reranker, Compiler)       |  |
|  |  Durable Task Scheduler                                         |  |
|  |  Local Execution Sandbox / Tool Implementations                 |  |
|  +-----------------------------------------------------------------+  |
+-----------------------------------------------------------------------+
```

* **Interface Boundary**: Handles protocol translation, channel affinity, token authentication, and rate limiting.
* **Manager/Skill Boundary**: Enforces strict JSON-schema task contracts. Prohibits direct skill-to-skill peer communication.
* **Intelligence/Trust Boundary**: Intercepts all proposed operations. Evaluates policy, user approvals, and permissions deterministically.
* **Trust/Tool Boundary**: Executes vetted actions against system APIs, filesystem, networks, and child processes using least-privilege tokens.
* **Storage/Derived Index Boundary**: Authoritative relational data is protected from destructive derived-index synchronization failures.

---

## 4. Component Responsibilities

| Component | Core Responsibility | Non-Responsibilities |
| :--- | :--- | :--- |
| **Central Manager** | Global reasoning, user intent parsing, context assembly request, skill orchestration, response synthesis. | Direct execution of system APIs, credential storage, final security authorization. |
| **Skills** | Specialized domain execution (e.g., Workflow, Vision, Code Analysis) via defined task contracts. | Direct cross-skill coordination, modifying security policies, bypassing the Manager. |
| **Trust Core** | Deterministic evaluation of permissions, autonomy levels, user approvals, and audit trail generation. | Semantic parsing of prompts, NLP reasoning, model inference. |
| **Tool Broker** | Sandboxed execution of approved tools, environment containment, output sanitization. | Deciding whether a tool *should* run (delegated to Trust Core). |
| **Credential Broker** | Secure storage, cryptographic isolation, and scoped issuance of API keys and secrets. | Exposing raw credentials to model prompts or skill payloads. |
| **Context Engine** | Multi-stage filtering, exact/semantic retrieval, reranking, and compiling the Context IR. | Deciding task planning or executing model queries directly. |
| **Memory / Second Brain** | Authoritative relational persistence of facts, events, decisions, entities, relations, and projects. | Generating natural language summaries or subjective opinions. |
| **Workflow Skill** | State tracking, step advancement, execution monitoring, and history tracking of workflow instances. | Destructive physical deletion of project databases (delegated to Trust Core). |
| **Task Scheduler** | Durable persistence, timer handling, recurrence calculation, and cron dispatch of deferred tasks. | Dynamic workflow path replanning. |
| **Hardware Provisioner** | Hardware benchmark execution, profile caching, model selection, quantization sizing. | Runtime skill task scheduling. |

---

## 5. Data Ownership Rules

1. **Global Data**:
   * Global entities, system configuration, user profile invariants, base personalization preferences, and unattached knowledge items are owned by the Root System Domain.
   * Global data persists independently of any specific project lifecycle.
2. **Project-Owned Data**:
   * Any task, workflow instance, memory fact, decision, artifact, or temporal session explicitly tagged with a `project_id` is strictly owned by that project.
3. **Cascade Deletion Invariant**:
   * Deletion of a Project cascades unconditionally to all project-owned tasks, workflow instances, project decisions, project-scoped memories, project artifacts, and project-specific derived indexes.
   * **Global Memory Preservation**: Global knowledge, cross-project learnings, and unattached user facts must NOT be deleted when a project is deleted, unless the entity is explicitly and exclusively owned by the target project.
4. **Derived vs. Authoritative Ownership**:
   * Vector embeddings, BM25/FTS indexes, and graph adjacency caches are derived assets owned by the indexing subsystem. Their deletion or corruption does not affect authoritative records.

---

## 6. Manager Responsibilities

The Central Manager / Supervisor is the cognitive conductor of AEGIS:
* **Single Front-Door**: Receives all normalized inputs across communication channels.
* **Intent & Goal Formulation**: Deconstructs complex user requests into discrete, verifiable operational goals.
* **Context Request & Receipt**: Issues queries to the Context Engine and receives a structured Context IR.
* **Task Contract Dispatch**: Generates strongly typed task contracts for downstream skills.
* **Synthesis & Verification**: Inspects skill execution results, evaluates output against acceptance criteria, and synthesizes the user-facing response.
* **Failure Handling & Replanning**: Detects skill execution errors, assesses retry budgets, and replans alternative strategies.
* **Channel Dispatch**: Emits user-facing output formatted specifically for the originating communication channel.

---

## 7. Skill Architecture

Skills are modular, isolated capability execution units.

### 7.1 Contract-Based Communication
Every skill interaction adheres to a formal Task Contract schema:
```json
{
  "task_id": "uuid-v4",
  "project_id": "optional-uuid",
  "skill_name": "target_skill_identifier",
  "action": "execute_action_name",
  "parameters": {},
  "constraints": {
    "timeout_ms": 30000,
    "max_memory_mb": 512,
    "network_permitted": false
  },
  "context_slice": {}
}
```

Skills return a structured Task Result:
```json
{
  "task_id": "uuid-v4",
  "status": "COMPLETED | FAILED | ESCALATED | REJECTED",
  "data": {},
  "artifacts": [],
  "error": null,
  "telemetry": {
    "duration_ms": 124,
    "tokens_consumed": 0
  }
}
```

### 7.2 Skill Execution Rules
* Skills do not invoke other skills directly. All data flows back to the Manager.
* Skills cannot alter their own capability bounds or request elevated permissions dynamically without Manager-mediated Trust Core re-evaluation.
* Skills operate within isolated execution contexts (subprocesses or constrained runtime environments).

---

## 8. Context Engine Architecture

The Context Engine prepares minimal, high-signal prompt context for the Manager and skills, eliminating unnecessary token expenditure.

### 8.1 Concept of the Internal AEGIS Context IR
The Context IR (Intermediate Representation) is a strongly typed, structured payload passed to the cognitive models instead of raw document dumps:
```json
{
  "active_goal": "String",
  "task_state": {},
  "project_state": {},
  "active_preferences": [],
  "relevant_facts": [],
  "decisions": [],
  "relevant_relationships": [],
  "recent_events": [],
  "evidence": [],
  "constraints": [],
  "unresolved_items": []
}
```

### 8.2 Conceptual Retrieval Path
Every context retrieval follows a deterministic multi-stage pipeline:
```
Query 
  --> 1. Scope Filtering (Project / Global / Temporal bounds)
  --> 2. Temporal Filtering (Recency windows, validity timestamps)
  --> 3. Exact Retrieval (FTS5 token matching, keyword search)
  --> 4. Semantic Retrieval (Vector cosine/distance matching)
  --> 5. Relationship Retrieval (Graph adjacency, entity-relation hops)
  --> 6. Reranking (Cross-encoder or reciprocal rank fusion scoring)
  --> 7. Deduplication (Content hash & semantic equivalence collapse)
  --> 8. Context Compilation (Token budgeting, structured IR assembly)
  --> Compact Manager Context IR
```

---

## 9. Memory / Second Brain Architecture

AEGIS rejects plain text or unstructured Markdown files as authoritative memory. It employs a **machine-first, structured schema**.

### 9.1 Authoritative Memory Categories
1. **Active Working Context**: Short-lived scratchpad of the immediate reasoning cycle.
2. **Session State**: Channel interaction states, message sequences, and volatile turns.
3. **Events**: Immutable, append-only chronological log of system, user, and agent occurrences.
4. **Facts**: Normalized, verified domain propositions and truths with confidence ratings.
5. **Preferences**: Explicit and derived user preferences with source attributions.
6. **Decisions**: Explicit choices made by the user or agent, including rationale, context, and outcomes.
7. **Experiences & Lessons**: Post-task evaluations, error reflections, and operational adaptations.
8. **Procedures**: Repeatable multi-step methods and procedural recipes.
9. **Projects**: Formal project entities and lifecycle states.
10. **Tasks**: Actionable units of work with dependency graphs.
11. **Workflows**: Defined sequences of procedural execution.
12. **Entities & Relations**: Knowledge graph vertices (people, organizations, tools, concepts) and edges.
13. **Evidence**: Source quotes, file citations, and cryptographic hashes supporting persisted facts.

### 9.2 Background Indexing & Consolidation
* High-frequency updates commit to authoritative relational stores immediately.
* Vector embeddings and graph projections are generated asynchronously in low-priority background workers.
* Periodic consolidation jobs merge redundant facts, expire transient sessions, and recalculate relationship weights.

---

## 10. Project Architecture

Projects are first-class organizational objects.

### 10.1 Project Schema Boundary
A project encapsulates:
* **Metadata**: UUID, name, description, priority, lifecycle status (`ACTIVE`, `PAUSED`, `ARCHIVED`, `DELETED`).
* **Context**: Project-specific domain rules, constraints, and scope boundaries.
* **Workflows & Tasks**: Directed acyclic graphs (DAGs) of project work.
* **Decisions & Memories**: Project-scoped historical records.
* **Relations & Artifacts**: File links, build artifacts, external resource pointers.
* **Permissions**: Scoped tool access and external resource whitelists.

### 10.2 Cascade Deletion
* Project deletion initiates a deterministic purge of all rows where `project_id == target_project_id`.
* All derived embeddings and search index entries tagged with `project_id` are purged.
* Global memory remains strictly preserved.

---

## 11. Workflow Architecture

Workflows represent structured, repeatable procedures distinct from projects and tasks.

### 11.1 Structural Hierarchy
```
Project
  â””â”€â”€ Workflow (Template / Definition)
        â””â”€â”€ Workflow Instance (Concrete Run)
              â””â”€â”€ Tasks (Executable Units)
                    â””â”€â”€ Events & Artifacts (Outputs)
```

### 11.2 The Workflow Skill
The Workflow Skill manages:
* Creation, editing, and versioning of workflow definitions.
* Instantiation and state advancement of workflow runs.
* Persistent scheduling of recurring workflows.
* Run history, metrics collection, and telemetry logging.
* Exposing project management operations through structured tools.

*Destructive database deletion of projects and workflows is strictly gated through the Trust Core and cannot be performed autonomously by the Workflow Skill.*

---

## 12. Scheduling Architecture

AEGIS requires durable, power-cycle-resilient scheduling.

* **Durable Task Scheduler**: Persists schedules in authoritative storage. Scheduled jobs survive application restarts and host reboots.
* **Execution Modes**:
  * One-shot timers (e.g., delay 2 hours).
  * Recurring cron schedules (e.g., daily 09:00 summary).
  * Event-driven triggers (e.g., upon new file detection).
* **Missed Execution Policy**: If the host was powered off during a scheduled execution window, the scheduler deterministically assesses the job's `catch_up_policy` (`RUN_ONCE`, `SKIP`, or `ALERT`).

---

## 13. Hardware Benchmark Architecture

To support hardware-aware model provisioning, AEGIS runs an automated hardware assessment.

### 13.1 Benchmark & Profiling Pipeline
```
Hardware Detection (CPU, RAM, GPU, VRAM, Storage)
  --> Compute Benchmark (Matrix multiply, FP16/INT4 TFLOPS estimation)
  --> Memory Bandwidth Benchmark (GB/s transfer rates)
  --> Storage I/O Benchmark (Sequential & random read/write IOPS)
  --> Saved Hardware Profile
```

### 13.2 Profile Caching Invariants
* The hardware profile is persistently cached.
* **Rebenchmark Trigger Conditions**: Benchmarks are re-run *only* when:
  1. A change in CPU, total RAM, or primary GPU is detected.
  2. The GPU driver version changes.
  3. The core AI execution runtime version changes.
  4. The user explicitly requests a forced rebenchmark.

---

## 14. Model Provisioning Architecture

Model allocation is dynamically fitted to host capacity:
1. **User Capability Selection**: The user selects desired capabilities and skills (e.g., Text Reasoning, Code Generation, Vision Analysis, Audio Transcription). Internal model architectures and specific checkpoint implementations remain abstracted from standard user interaction.
2. **Compatibility Determination**: The provisioner checks the cached hardware profile against model requirement tables (parameter counts, quantization requirements, context window VRAM usage, instruction set requirements).
3. **Storage & Resource Validation**: Verifies local disk free space and system RAM overhead before downloading or activating any model.
4. **Provisioning Plan Generation**: Emits a deterministic configuration specifying model sizes, quantization (e.g., Q4_K_M, Q8_0), and runtime placement (GPU offload layers vs. CPU inference) in accordance with policy.

---

## 15. Model Lifecycle Architecture

Model deployment, active residency, and updates are strictly orchestrated.

### 15.1 Model Update Policies

#### Normal Skill Model Update Policy
1. Delete the previous skill model.
2. Download the new model.
3. Verify integrity (cryptographic hash / checksum).
4. Run compatibility / skill verification test.
5. Activate the new model.
6. If the new model fails verification or testing, mark the skill unavailable and report the failure.

#### Manager Model Update Policy
1. Keep the current Manager model active and operational.
2. Download the candidate Manager model first into isolated staging.
3. Verify integrity (cryptographic hash / checksum).
4. Load the candidate Manager model in a test sandbox.
5. Run Manager automated self-test suite.
6. Activate the new Manager model (switch routing).
7. Run post-activation health verification.
8. Only after successful activation and verified health confirmation may the previous Manager model be deleted.

*Manager availability has absolute highest model lifecycle priority. This rule must never be weakened due to local storage constraints.*

### 15.2 Runtime VRAM Arbitration
When VRAM is insufficient to hold all models concurrently:
* The Manager model has first claim on persistent VRAM residency.
* Skill models (e.g., vision, audio, heavy code generation) are loaded dynamically on-demand and evicted when idle according to an LRU cache policy.

---

## 16. Autonomy / Approval Architecture

AEGIS employs an adaptive, deterministic autonomy framework.

### 16.1 Operational Autonomy Tiers
AEGIS freezes four operational autonomy tiers:
* **Tier 0 — Strict Manual Approval**: Every tool invocation, filesystem write, external query, and state-mutating operation requires explicit user confirmation.
* **Tier 1 — Safe / Bounded Execution**: Read-only operations, local sandbox computations, and safe diagnostic queries execute autonomously; all state-mutating, network, or filesystem modifications require approval.
* **Tier 2 — Supervised Autonomy**: Pre-approved routine operations (e.g., editing files within designated project directories, local linting, running unit tests) proceed autonomously; high-impact, destructive, or external communications require approval.
* **Tier 3 — Full Bounded Task Autonomy**: Bounded autonomous execution within an approved workflow scope and budget; emergency halts, policy constraints, and fail-closed security triggers remain continuously active.

*Action-risk classification remains strictly separate from autonomy tiers. The autonomy tier is an operational policy configuration, not a substitute for deterministic security boundaries.*

### 16.2 Approval Flow
```
Skill Action Proposal
  --> Trust Core Policy Evaluation
  --> Requires Approval?
        |-- NO  --> Proceed to Tool Broker
        â””â”€â”€ YES --> Emit Approval Request to User Channel
                      |-- APPROVED --> Execute & Log
                      |-- REJECTED --> Cancel & Return Rejection to Manager
                      â””â”€â”€ TIMEOUT  --> Fail-Closed (Abort action)
```

---

## 17. Trust Core Boundary

The Trust Core is a deterministic software layer completely outside the LLM reasoning process.

* **Invariants**:
  * Operates via deterministic code (no prompt engineering, no LLM in the authorization loop).
  * Validates cryptographic signatures, tokens, and policy definitions.
  * Enforces fail-closed semantics: any error, ambiguity, or unexpected state results in immediate denial of execution.
  * Generates tamper-resistant, append-only audit logs for all security-relevant decisions.

---

## 18. Tool Boundary

The Tool Boundary mediates all interaction between AEGIS intelligence and external runtime systems.

* **Tool Broker**:
  * Maintains an explicit whitelist of registered tool definitions.
  * Enforces strict argument validation via JSON Schema before execution.
  * Spawns tools in isolated subprocesses with restricted environment variables, process priority, and working directories.
  * Sanitizes all tool stdout/stderr streams to prevent prompt injection payload delivery into subsequent context compilation.

---

## 19. Credential Boundary

* **Credential Broker**:
  * Securely manages secrets, API tokens, passwords, and private keys.
  * Raw credentials are never injected into LLM prompt contexts.
  * When an external API requires authentication, the Tool Broker delegates the network request to the Credential Broker, which injects the header or signature directly into the outgoing HTTP request at the transport layer.
  * Models and skills interact only with opaque credential references (e.g., `cred://openai/default`).

---

## 20. Network Boundary

* **Controlled Outbound Access**:
  * AEGIS runs with default-deny network rules for skills and tools.
  * Network egress requires explicit whitelist authorization per project and tool.
* **Tainted Ingress**:
  * All inbound network responses (web pages, API responses, downloaded files) are classified as untrusted data.
  * Ingress content must pass through content sanitizers (stripping executable code, hidden script tags, and prompt injection vectors) before indexing or Context IR compilation.

---

## 21. Backup Architecture

AEGIS backups are primarily **user-content backups**. The backup system strictly distinguishes **Authoritative User Content** from **Rebuildable Internal Data / AI Infrastructure**.

### 21.1 Included in Backups (Authoritative User Content)
* User-created context documents.
* Projects and project metadata.
* Workflows and workflow definitions.
* Project-owned and user-owned artifacts.
* Task and workflow execution data required to restore user work.
* Other explicitly user-owned AEGIS content.

### 21.2 Explicitly Excluded from Backups (Rebuildable Internal Data & Infrastructure)
* AI model weight files (Ollama / GGUF / ONNX binaries).
* Python virtual environments and installed package runtimes.
* Native runtime binaries and compiled toolchains.
* Hardware benchmark results and cached hardware profiles.
* Rebuildable search and vector indexes (FTS5 databases, vector stores, graph adjacency caches).
* Temporary session caches, scratch directories, and transient inference data.
* Internal ephemeral logs, token telemetry, and intermediate execution traces not required to restore user work.

### 21.3 Backup Targets
1. **Local Recovery Backup**: Encrypted, versioned snapshot written to an isolated local directory or secondary storage drive.
2. **Cloud Recovery Backup**: Provider-independent backup subsystem. Initial reference provider: Google Drive (OAuth2 PKCE, encrypted archive).

---

## 22. Privacy and Deletion Architecture

* **Right to be Forgotten & Selective Purge**: Users can query, inspect, and delete individual facts, entire sessions, specific projects, or the entire memory substrate.
* **Project Cascade Deletion**:
  * Project deletion executes an atomic transaction purging all project records.
  * The deletion subsystem purges project entries from the relational store, vector store, search indexes, and local artifact directories.
  * Trust Core logs the purge event for auditable compliance.
* **Global vs. Project Isolation**: Global facts remain unperturbed unless explicitly tagged with the target project identifier.

---

## 23. Channel Architecture

AEGIS interacts across multiple user endpoints while maintaining unified backend state.

### 23.1 Channel-Affine Routing
* Interactions are strictly channel-affine:
  * A WhatsApp request receives its response via WhatsApp.
  * A Desktop request receives its response via Desktop.
* The underlying shared backend state (memory, projects, tasks, workflows, permissions) remains global and unified.
* AEGIS does not automatically mirror normal conversations between channels.

### 23.2 Explicit Handoff
* A workflow or task initiated on one channel (e.g., WhatsApp mobile) may be explicitly handed off to another (e.g., Desktop PC) via a signed handoff token or Manager command when deep local tool execution (e.g., heavy file manipulation or compilation) is required.

---

## 24. Notification Architecture

* **Event Bus**: The notification subsystem subscribes to internal task completion, failure, and escalation events.
* **Channel Routing Rules**:
  * Normal conversational responses remain strictly channel-affine.
  * Cross-channel notification delivery (e.g., broadcasting an alert across channels or delivering an update to WhatsApp while a desktop task runs) is permitted **only through explicit user notification policy**.
  * Without explicit user configuration, notifications route strictly to the originating channel of the associated task or active project session.
* **Quiet Hours & Throttling**: Configurable notification frequency limiting prevents notification spam during automated batch runs.

---

## 25. Offline / Online / Hybrid Architecture

AEGIS adapts to network availability:
* **Local-First / Offline Operation**:
  * Core reasoning (via local quantized models), memory retrieval, local tool execution, and desktop task management function fully without internet connectivity.
* **Online Operation**:
  * When internet connectivity is active and authorized, external web search, cloud backups, remote API calls, and cloud-hosted LLM endpoints are enabled.
* **Hybrid Dynamic Routing**:
  * If local VRAM/compute is constrained and remote API access is authorized, non-sensitive tasks can be routed to cloud models, while private/sensitive project tasks remain strictly on local models.

---

## 26. Performance Architecture

Performance and minimal latency are foundational requirements:
1. **Hot Context Caching**: Reusable prompt prefixes (system invariants, personalization) are structured to maximize model KV-cache hit rates.
2. **Project-Scoped Retrieval**: Queries target specific `project_id` namespaces first, drastically reducing search space in vector and FTS indexes.
3. **Multi-Stage Context Pipeline**: Combines rapid FTS5 exact matching with semantic vector search and graph hops, followed by reranking, passing only the top-K items to Context IR compilation.
4. **Asynchronous Indexing**: Embedding generation and graph edge calculations run in asynchronous, low-priority background workers, ensuring conversational interactions are never blocked.
5. **Rebuildable Derived Indexes**: Storage engines use lightweight, append-friendly designs, safe in the knowledge that corrupt indexes can be rebuilt from authoritative records.

---

## 27. Recovery Architecture

* **Crash-Resilient State Engine**: All task and workflow state transitions are recorded in write-ahead logged (WAL) authoritative transactions before execution.
* **Startup Reconciliation**:
  * Upon startup, the Reconciliation Engine scans for in-flight tasks marked `RUNNING`.
  * Incomplete tasks are inspected against idempotent checkpoints and either resumed, safely rolled back, or flagged for user attention.
* **Database Disaster Recovery**: In the event of derived index corruption, an automatic recovery protocol reads authoritative relational records and re-indexes the vector and text stores.

---

## 28. Data Lifecycle

```
Ingress (User input / Tool result)
  --> Sanitization & Taint Tagging
  --> Active Session Working Memory
  --> Task Execution & Result Synthesis
  --> Authoritative Persistence (Events, Facts, Decisions)
  --> Asynchronous Background Indexing (Embeddings, FTS, Graph)
  --> Consolidation & Aging (Decay, Reinforcement, Archival)
  --> Explicit Deletion / Cascade Purge
```

---

## 29. Major Invariants

1. **Security Invariant**: No LLM output can directly invoke a system or network call without traversal through the deterministic Trust Core and Tool Broker.
2. **Memory Invariant**: The Manager never receives the raw, unbounded historical database; it interacts solely with the compiled, token-bounded Context IR.
3. **Data Integrity Invariant**: Authoritative relational data is the single source of truth; derived indexes are strictly secondary and disposable.
4. **Project Deletion Invariant**: Deleting a project cascades to all project-owned data and derived assets; it must never delete unowned global knowledge.
5. **Lifecycle Invariant**: An active Manager model cannot be replaced or deleted until the incoming Manager passes end-to-end operational verification.
6. **Network Invariant**: Outbound network requests are denied by default unless granted by explicit, scoped policy whitelist.

---

## 30. Known Implementation Questions

1. **Local Database Engine Selection**:
   * Assessment needed on whether to utilize embedded SQLite (with FTS5 and sqlite-vec) vs. existing background services (MongoDB / MySQL) for machine-first memory persistence.
2. **Local Vector Storage Substrate**:
   * Evaluate embedded C/Python vector extensions (e.g., `sqlite-vec`, Faiss, or Chroma) that operate efficiently within Windows environments without requiring Docker.
3. **Windows File-Locking Mitigation**:
   * Establish strategies to handle Windows file-system locking during high-frequency SQLite WAL checkpointing, particularly given that the user's workspace resides inside a OneDrive directory.
4. **VRAM Eviction Latency**:
   * Measure the real-world latency of swapping 4-bit skill models in and out of 6 GB VRAM on Windows via the Ollama/llama.cpp backend.

---

## 31. Deferred Decisions

1. **Specific Model Weights & Quantizations**: Final choice of model families (e.g., Qwen-2.5, Llama-3.2, Mistral) is deferred until hardware benchmarking is formally executed.
2. **Vector Dimension Standards**: Exact embedding model and vector dimension selection are deferred until the embedding runtime is benchmarked.
3. **WhatsApp Transport Mechanism**: Decision between local WhatsApp Web automation vs. official Cloud API / Twilio gateway is deferred to channel implementation phase.
4. **Cloud Storage SDK Choice**: Google Drive API client implementation details are deferred until the backup subsystem phase.

---

## 32. Implementation Prerequisites

Prior to implementing AEGIS code and services, the following technical prerequisites must be established:
1. **Canonical Workspace Location & OneDrive Coordination**:
   * The canonical repository is established at `C:\Users\digvi\OneDrive\Desktop\project\aeigs`. Runtime state, high-frequency caches, SQLite WAL journals, and virtual environment structures must be coordinated with OneDrive synchronization to mitigate potential file-locking conflicts.
2. **Dedicated Isolated Virtual Environment**:
   * Creation of an isolated Python 3.12 virtual environment (`.venv`) dedicated exclusively to AEGIS to isolate dependencies from system packages.
3. **Repository Initialization**:
   * Initialization of the local Git repository, standard `.gitignore`, and foundational directory structure.
4. **Ollama Daemon Activation**:
   * Configuration of the local Ollama instance as a controllable background service.
5. **Execution Architecture & Schemas**:
   * Definition of JSON Schemas for Task Contracts, Task Results, and Context IR prior to writing functional logic.

---

## 33. NOT IMPLEMENTED YET

The following list explicitly enumerates components, modules, and assets that are **NOT IMPLEMENTED** at this stage:
* No application source code has been written.
* No Python packages or virtual environments have been created or installed for AEGIS.
* No AI models, GGUF weights, or checkpoints have been downloaded or installed.
* No database tables, schemas, or migrations have been executed.
* The Central Manager, Skills, and Tool Broker do not exist as executable code.
* The Trust Core, Policy Engine, and Credential Broker do not exist.
* The Context Engine, Reranker, and Vector Store do not exist.
* The Task Scheduler and Workflow Engine do not exist.
* Hardware benchmarking has not been executed; no hardware profile has been created.
* Channel adapters (Desktop GUI/CLI, WhatsApp) do not exist.
* Local and Cloud Backup subsystems do not exist.
* No Git commits or repository history have been created.

---
*End of Technical Foundation Specification.*
