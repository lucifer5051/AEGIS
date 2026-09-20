# AEGIS Performance Architecture

**Document Version**: 1.0.0  
**Status**: ACTIVE / ARCHITECTURAL EXTENSION  
**Author**: AEGIS Architecture & Core Foundation Team  
**Scope**: End-to-End Latency Reduction, Component Residency, Multi-Tier Caching, Resource Management, and Graceful Degradation  
**Canonical Repository**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. Executive Summary & Core Principle

AEGIS is designed as a local-first personal autonomous agent operating on edge hardware where computational resources (CPU cores, RAM, and GPU VRAM) are bounded. Achieving low latency and responsiveness requires an explicit performance architecture that prevents unnecessary model invocations, eliminates redundant computation, and minimizes critical-path latency.

### 1.1 The Core Performance Principle

> **"Do the cheapest correct thing first."**

The architectural execution sequence strictly prioritizes:
1. **Deterministic path** when rules or static configurations are sufficient.
2. **Cached path** when valid, unexpired, deterministic cached state exists.
3. **Bounded fast decision** when a lightweight heuristic or compact decision model can safely classify or route the request.
4. **Manager reasoning** only when multi-step synthesis, ambiguity resolution, or complex task decomposition is genuinely required.
5. **Expensive tools and heavyweight models** only when simpler, lower-overhead alternatives cannot satisfy the request.

### 1.2 The Performance Safety Invariant

Performance optimization must **NEVER** bypass or weaken:
- **Trust Core security and authorization gates**
- **User approval requirements (Tiers 0–3)**
- **Deterministic parameter and safety validation**
- **Step and artifact verification**
- **Tamper-evident audit logging**

Performance is strictly subordinate to security and correctness. An unauthorized or unverified action executed quickly is a critical system failure.

---

## 2. End-to-End Latency Optimization Dimensions

The performance architecture optimizes ten explicit latency and resource dimensions:

| Dimension | Target Optimization Strategy |
| :--- | :--- |
| **1. Response Latency** | Direct fast-path returns for read-only queries, cached states, and deterministic workflows. |
| **2. Routing Latency** | Sub-millisecond deterministic classification and bounded micro-second fast decisions over monolithic model prompt evaluations. |
| **3. Context Retrieval Latency** | Strict hierarchical scoping (project-bounded, entity-keyed, temporal windows) before vector or deep search. |
| **4. Model Load/Unload Latency** | Hot residency prioritization for the central Manager, warm pools for frequent skills, and lazy loading for cold utilities. |
| **5. Repeated-Task Latency** | Parametric workflow templates, compiled plan graphs, and input/output deterministic memoization. |
| **6. Memory Pressure** | Memory-mapped data stores, compact Context Intermediate Representation (IR), and streaming context pipelines. |
| **7. CPU/GPU Contention** | Workload queuing, single-flight compute reservations, and offloading non-critical tasks from acceleration runtimes. |
| **8. Background Task Isolation** | Strict separation of critical interactive paths from deferred maintenance, embedding, indexing, and cleanup. |
| **9. Tool Execution Latency** | Reusable subprocess environments, persistent local daemon adapters, and parallel execution of independent tool steps. |
| **10. End-to-End Latency** | Comprehensive pipeline stage telemetry, automated bottleneck identification, and percentile tracking (P50, P90, P95, P99). |

---

## 3. Canonical Critical-Path Architecture

The complete system execution pipeline establishes clear boundaries between the fast interactive path and complex reasoning:

```
                          [USER INPUT]
                               │
                               ▼
                    [INPUT NORMALIZATION]
                     (Sanitize, Tokenize)
                               │
                               ▼
                     [FAST PATH EVALUATION]
             ┌─────────────────┼─────────────────┐
             ▼                 ▼                 ▼
     [Deterministic]     [Cached State]    [Fast Decision]
      (Regex, Rules)     (Memoized Rsp)     (Lightweight)
             │                 │                 │
             └─────────────────┼─────────────────┘
                               │
                               ▼
                      [ROUTING DECISION]
        ┌──────────────┬───────┴───────┬──────────────┐
        ▼              ▼               ▼              ▼
  [Direct Exec]  [Skill Fast]    [Need Clarif]   [Invoke Manager]
        │              │               │              │
        │              ▼               ▼              ▼
        │        [TRUST CORE]    [User Prompt]   [CONTEXT RETRIEVAL]
        │      (Authorization)                        │
        │              │                              ▼
        │              ▼                       [MANAGER REASONING]
        │         [TOOL EXEC]                         │
        │              │                              ▼
        │              ▼                       [SKILL DISPATCH]
        │        [VERIFICATION]                       │
        │              │                              ▼
        └──────────────┼───────────────────────► [TRUST CORE]
                       │                       (Authorization)
                       ▼                              │
                  [RESPONSE]                          ▼
                       ▲                         [TOOL EXEC]
                       │                              │
                       └─────────────────────── [VERIFICATION]
```

---

## 4. Manager & Skill Residency Architecture

On resource-constrained hardware (e.g., 6 GB VRAM, 16 GB RAM), loading model weights into memory causes significant latency penalties (several seconds per cold start).

### 4.1 Manager Hot Residency

The Manager / Supervisor is the cognitive core of AEGIS. To ensure rapid time-to-first-thought:
- **Highest Runtime Priority**: The Manager maintains priority residency over all other intelligence models.
- **Persistent Memory Allocation**: When system resources permit, the Manager process and working weights remain pinned in host RAM or GPU VRAM.
- **Resource Pressure Monitoring**: If host physical RAM or VRAM exceeds the configured high-pressure threshold, the Manager may yield its acceleration budget but retains a hot CPU fallback state.
- **Lifecycle Compatibility**: Manager residency respects the established update protocol (download new -> sandbox test -> hot-swap activation -> health verification -> old Manager eviction).

### 4.2 Three-Tier Skill Residency Model

Skills are categorized dynamically into three residency tiers:

```
+-------------------------------------------------------------------------+
| HOT (Resident)                                                          |
| - Pinned in memory / active execution context                           |
| - Zero load latency; immediate tool invocation                          |
| - Examples: Core file operations, clipboard, active project workspace   |
+-------------------------------------------------------------------------+
                                    ▲
                                    │ (Dynamic Promotion / Demotion)
                                    ▼
+-------------------------------------------------------------------------+
| WARM (Staged / Standby)                                                 |
| - Python bytecode loaded, tool metadata cached, model weights on disk   |
| - Rapid warm-up latency (< 500ms)                                       |
| - Examples: Web retrieval adapter, document parser, Git utility        |
+-------------------------------------------------------------------------+
                                    ▲
                                    │ (Dynamic Eviction / On-Demand Fetch)
                                    ▼
+-------------------------------------------------------------------------+
| COLD (On-Demand Only)                                                   |
| - Completely unmapped from memory; metadata stored in SQLite/disk       |
| - Loaded only upon explicit task requirement; immediately evicted      |
| - Examples: Bulk migration tools, disaster recovery, system diagnostics |
+-------------------------------------------------------------------------+
```

Residency placement is governed by:
1. **Historical Execution Frequency**: Exponential moving average of usage.
2. **Startup / Load Overhead**: Heavy components with high load cost stay warm longer.
3. **Hardware Profile Constraints**: Current available RAM, VRAM, and memory load percentage.
4. **Active Task Graph**: Upcoming steps in a multi-step workflow trigger predictive warming.

---

## 5. Multi-Tier Cache Architecture

AEGIS implements an eight-domain cache architecture. Every cache is non-authoritative; authoritative state resides strictly in relational database tables or source configuration files.

| Cache Domain | Managed State | Max Lifetime | Invalidation Rule | Authority Source |
| :--- | :--- | :--- | :--- | :--- |
| **1. Configuration** | Parsed Pydantic config & resolved paths | Process lifetime | File modification time / env var change | `config/*.yaml` |
| **2. Hardware Profile** | Detected hardware specs & platform data | 24 Hours / Reboot | System restart or hardware mismatch | `Win32 CIM / sys` |
| **3. Project Context** | Active directory trees, git heads, manifests | Task lifetime / 10m | File system watch events (`Watchdog`) | Local workspace |
| **4. Memory Retrieval** | Entity lookups, recent query responses | 15 Minutes | New memory write or entity update | SQLite EKM tables |
| **5. Parsed Artifacts** | ASTs, markdown structures, JSON schemas | 1 Hour | Source file content hash mismatch | Disk files |
| **6. Workflow Defs** | Validated task DAGs, parameter contracts | 1 Day | Workflow file revision change | Workflow manifests |
| **7. Tool Metadata** | Capability schemas, CLI paths, versions | 12 Hours | Tool binary modification or path change | Tool registrations |
| **8. Runtime State** | Process handles, sub-agent thread pools | Active session | Subprocess termination or health fail | OS process table |

### 5.1 Cache Invariants
- **No Authoritative Data in Cache**: Evicting or wiping any cache domain must result in zero data loss; the system must rebuild state transparently from authoritative stores.
- **Strict Size Bounds**: Every cache enforces deterministic capacity limits (LRU / FIFO) to prevent unbounded memory growth.

---

## 6. Context Pipeline & Retrieval Performance

Constructing context for reasoning models is a major contributor to latency and memory usage.

### 6.1 Stable Prefix + Dynamic Suffix

To maximize KV-cache reuse in local inference engines and eliminate repetitive tokenization overhead:
- **Stable Prefix**: Static system rules, core identity, invariant security constraints, and canonical tool schemas are structured into an immutable prefix block.
- **Dynamic Suffix**: Variable task-specific data, retrieved memory facts, recent conversation turns, and tool outputs are appended strictly after the stable prefix.

```
[===================== STABLE PREFIX =====================] [====== DYNAMIC SUFFIX ======]
│ System Invariants │ Core Principles │ Tool Definitions  │ │ Task Query │ Context │ Temp │
[===================== (KV CACHED) =======================] [====== (TOKENIZED FRESH) ===]
```

### 6.2 Streamlined Memory Retrieval Pipeline

Retrieval operates through a funnel designed to minimize latency:

```
Query -> Scope Filtering (Active Project/Domain)
      -> Exact Key/Entity Lookup (< 5ms)
      -> Semantic Search (Top-K embeddings, only if unresolved)
      -> Cross-Encoder Reranking
      -> Deduplication & Provenance Pruning
      -> Context Token Budget Enforcement
      -> Compact Context IR
      -> Delivered to Manager Context Window
```

Full-history conversational scans are strictly forbidden during standard task execution.

---

## 7. Concurrency & Parallel Execution

When a complex goal is decomposed by the Manager into a directed acyclic graph (DAG) of tasks, independent steps execute in parallel:

```
                     Manager DAG Decomposition
                                 │
                 ┌───────────────┼───────────────┐
                 ▼               ▼               ▼
           [Task Node A]   [Task Node B]   [Task Node C]
            (Read Docs)     (Fetch Data)    (Check Lint)
                 │               │               │
                 ▼               ▼               ▼
           [Trust Core]    [Trust Core]    [Trust Core]
           (Capability)    (Capability)    (Capability)
                 │               │               │
                 ▼               ▼               ▼
           [Tool Exec]     [Tool Exec]     [Tool Exec]
                 │               │               │
                 └───────────────┼───────────────┘
                                 │
                                 ▼
                     [DAG Aggregation & Sync]
                                 │
                                 ▼
                    [Manager Step Verification]
```

### 7.1 Concurrency Rules
- **Dependency Strictness**: Tasks with unresolved dependencies never execute concurrently.
- **Per-Task Trust Core Gating**: Every parallel thread independently submits tool requests to Trust Core; concurrency does not bypass individual action authorization.
- **Resource Concurrency Throttling**: The maximum number of parallel worker threads is bounded by the detected hardware profile (CPU core count and free RAM).

---

## 8. Background Execution Runtime

To protect interactive user responsiveness, non-critical background jobs are decoupled from the user interaction thread:

### 8.1 Qualified Background Workloads
- Vector embedding generation and semantic index building
- Entity extraction, memory deduplication, and historical consolidation
- Proactive cache warming and AST pre-parsing
- System diagnostics, disk hygiene, and log rotation
- Non-urgent remote synchronization or external status checks

### 8.2 Background Isolation Invariant
- **Interactive Priority**: Interactive user commands take absolute precedence over background queues.
- **Throttling under Contention**: If CPU utilization exceeds 85% or memory pressure is high, background workers yield immediately (cooperative backoff).
- **Correctness Dependency**: The user response path only awaits a background job if that specific job's output is required for factual correctness.

---

## 9. Resource-Aware Degradation States

AEGIS adapts its operational profile dynamically based on the current hardware state detected by the Hardware Detection Subsystem:

```
+-------------------+--------------------------------------------------------------------+
| System State      | Operational Profile & Performance Adjustments                       |
+-------------------+--------------------------------------------------------------------+
| FULL              | Standard Fast Path active; Manager hot resident in VRAM/RAM;      |
|                   | maximum allowed concurrency enabled; full background workers on.   |
+-------------------+--------------------------------------------------------------------+
| CONSTRAINED       | Concurrency halved; background worker batch sizes reduced;         |
|                   | memory cache thresholds tightened.                                 |
+-------------------+--------------------------------------------------------------------+
| RESOURCE_PRESSURE | All COLD and WARM components evicted; Manager offloaded to CPU;    |
|                   | background indexing and embeddings completely paused.              |
+-------------------+--------------------------------------------------------------------+
| LOW_MEMORY        | Strict single-thread execution; Context IR pruned to minimal       |
|                   | tokens; aggressive LRU cache flushing; disk swap avoidance.       |
+-------------------+--------------------------------------------------------------------+
| OFFLINE           | Remote decision providers disabled; fallback to local heuristic    |
|                   | and local Manager execution; zero outbound network checks.        |
+-------------------+--------------------------------------------------------------------+
```

---

## 10. External Technology Policy (Jev & Beyond)

1. **Abstraction Independence**: The Fast Decision Engine and Fast Path are native, self-contained AEGIS architectural abstractions.
2. **Zero Hard Dependencies**: AEGIS is **NOT** dependent on Jev, proprietary cloud routers, or external model gateways.
3. **Evaluation Protocol**: Any future evaluation of external decision engines or frameworks must:
   - Operate as an optional, interchangeable provider behind the `FastDecisionEngine` interface.
   - Strictly satisfy local-first, privacy, and zero-data-leakage mandates.
   - Maintain 100% functional fallback to deterministic local rules when offline.
   - Demonstrate measurable empirical latency and accuracy advantages through objective benchmarks before adoption.
