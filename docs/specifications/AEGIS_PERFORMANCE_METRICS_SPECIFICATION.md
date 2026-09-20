# AEGIS Performance Metrics & Telemetry Specification

**Document Version**: 1.0.0  
**Status**: ACTIVE / FOUNDATIONAL SPECIFICATION  
**Author**: AEGIS Architecture & Core Foundation Team  
**Scope**: Low-Overhead Pipeline Latency Instrumentation, Event Schemas, and Percentile Tracking  
**Canonical Repository**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. Objectives & Principles

The AEGIS Performance Metrics Subsystem provides deterministic, high-resolution, zero-overhead telemetry across every stage of the execution lifecycle. Performance cannot be managed without continuous, empirical measurement.

### 1.1 Core Telemetry Principles
1. **Low Overhead**: Telemetry logging must introduce less than 0.5ms of latency per transaction.
2. **Zero-Secret Invariant**: Performance metrics capture timestamps, durations, counts, and stage labels. Under **no circumstances** may user prompts, API tokens, passwords, document contents, or sensitive data be logged to telemetry streams.
3. **Correlation Integrity**: Every pipeline event is tied to a deterministic `trace_id` and `task_id` for end-to-end lineage.
4. **Offline Isolation**: Telemetry data is stored locally in temporary SQLite event tables or circular log buffers; no external analytics pings are transmitted.

---

## 2. Measurable Pipeline Stages

Every interactive request is decomposed into discrete, non-overlapping or correlated stages:

| Stage Identifier | Measurement Scope | Typical Critical Path |
| :--- | :--- | :--- |
| `INPUT_NORMALIZATION` | String sanitization, token length calculation, whitespace normalization. | Yes |
| `CACHE_LOOKUP` | Probing Configuration, Workflow, or State caches for hits. | Yes |
| `FAST_PATH_DECISION` | Execution of rule tables, regex maps, or local decision classifier. | Yes (Fast Path) |
| `MEMORY_RETRIEVAL` | Scope filtering, exact key lookup, semantic vector search, reranking. | Conditional |
| `CONTEXT_ASSEMBLY` | Merging Stable Prefix with Dynamic Suffix into Context IR. | Conditional |
| `MANAGER_LOAD` | Time to ensure Manager model weights and execution context are resident. | Conditional |
| `MANAGER_INFERENCE` | Time-to-first-token and complete reasoning synthesis by the Manager. | Conditional |
| `SKILL_LOAD` | Dynamically loading skill module or warming worker subprocess. | Yes |
| `TOOL_EXECUTION` | Time elapsed inside tool subprocess or native function call. | Yes |
| `TRUST_CORE_AUTH` | Deterministic capability validation, policy check, credential injection. | Yes |
| `STEP_VERIFICATION` | Output schema validation, post-condition check, artifact verification. | Yes |
| `RESPONSE_ASSEMBLY` | Formatting output, rendering user message, updating channel state. | Yes |
| `END_TO_END` | Wall-clock elapsed duration from user receipt to final output return. | Complete |

---

## 3. Telemetry Event Schema

Individual stage measurements emit structured records conforming to the canonical schema:

```json
{
  "event_id": "evt-8b91a2c3-10d4",
  "trace_id": "trc-f0e9d8c7-20b1",
  "task_id": "tsk-a1b2c3d4-30e5",
  "stage_name": "FAST_PATH_DECISION",
  "execution_path": "FAST_PATH",
  "timestamp_start": "2026-09-20T18:00:00.123456Z",
  "timestamp_end": "2026-09-20T18:00:00.135456Z",
  "duration_ms": 12.0,
  "status": "SUCCESS",
  "hardware_context": {
    "memory_load_percent": 82,
    "cpu_busy_percent": 24,
    "vram_used_mb": 4200
  },
  "metadata": {
    "decision_type": "ROUTE_DISPATCH",
    "selected_skill": "workspace_file_reader",
    "cache_hit": false
  }
}
```

---

## 4. Aggregated Statistical Metrics

The Telemetry Engine computes rolling percentiles and efficiency rates across configurable sliding windows (e.g., last 100 requests, last 1 hour):

### 4.1 Percentile Latency Distributions
- **P50 (Median)**: Baseline expected responsiveness for standard queries.
- **P90**: Typical upper bound for complex or multi-skill interactions.
- **P95**: Tail latency indicator under moderate resource pressure.
- **P99**: Extreme outlier threshold revealing model reload spikes or disk I/O bottlenecks.

### 4.2 System Efficiency Metrics
- **Cache Hit Rate**: Percentage of queries resolved without storage/model compute:
  $$\text{Hit Rate} = \frac{\text{Hits}}{\text{Hits} + \text{Misses}} \times 100\%$$
- **Manager Residency Rate**: Percentage of Manager invocations where the model was already resident in memory/VRAM.
- **Unnecessary Manager Invocation Rate**: Percentage of Manager calls that resulted in trivial single-step routing that Fast Path could have satisfied.
- **Time-to-First-Response (TTFR)**: Milliseconds elapsed until initial interactive streaming feedback is displayed.
- **Time-to-Complete (TTC)**: Milliseconds elapsed until final task verification and state settlement.

---

## 5. Performance Budgets

Conceptual latency and resource allocations:

| Workload Category | Target Latency Budget | Concurrency Cap | Memory Ceiling |
| :--- | :--- | :--- | :--- |
| **Deterministic Command** | < 50ms | Unrestricted | Minimal |
| **Cached Query** | < 10ms | Unrestricted | Buffer only |
| **Fast-Path Skill Execution**| < 300ms | 4 Workers | < 500 MB RAM |
| **Manager Planning Loop** | Configurable (Model dependent) | 1 Active Inference | Reserved Budget |
| **Background Maintenance** | Yielding / Unscheduled | 1 Worker | < 250 MB RAM |

*Note: Numerical latency targets for model-dependent stages are calibrated following Phase 3 hardware benchmarks.*
