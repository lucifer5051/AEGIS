# AEGIS Hardware Benchmark Subsystem Specification

**Document Version**: 1.0.0  
**Status**: ACTIVE / FOUNDATIONAL SPECIFICATION  
**Author**: AEGIS Architecture & Core Foundation Team  
**Scope**: Deterministic, Controlled Hardware Performance Characterization and Profile State Transitions  
**Canonical Repository**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. Purpose and Philosophy

The AEGIS Hardware Benchmark Subsystem provides controlled, bounded, deterministic measurement of actual host hardware performance. Its objective is not to produce a synthetic leaderboard score, but to characterize available computational capacity for future resource budgeting, concurrency caps, and model provisioning.

> [!IMPORTANT]
> **Core Architectural Distinctions**:
> 1. **Detection != Benchmarking**: Hardware detection (Prompt 007) inspects static topology (what hardware exists). Benchmarking (Prompt 010) measures actual execution throughput (how fast it executes).
> 2. **Benchmarking != Provisioning**: Benchmarking produces empirical performance evidence. Model provisioning (Phase 3) later consumes this evidence to select quantization levels and memory budgets.
> 3. **Performance != Security Authority**: Benchmark results never grant capabilities, bypass Trust Core, or weaken user approval requirements.

---

## 2. Safety and Resource Boundaries

Benchmarking operates under strict safety invariants:
- **Bounded Duration**: Workloads are finite, deterministic, and enforce strict execution timeouts (default 10s–15s per phase).
- **Cancellable**: Supports non-blocking cooperative cancellation checks at every iteration.
- **Resource Aware**: Monitors physical RAM before allocation; automatically downscales buffers if free memory drops below safe operating thresholds (< 800 MB).
- **Read-Only with Respect to System**: Never alters OS power plans, registry keys, GPU clocks, fan curves, or hardware settings.
- **Strict Storage Hygiene**: Tests storage only inside AEGIS-controlled scratch directories using dedicated temporary files cleaned up immediately in `finally` blocks.
- **100% Offline**: Zero external network requests, zero telemetry uploads.

---

## 3. Benchmark Modes & Levels

The subsystem defines four operational benchmark levels:

| Level | Identifier | Workload Scope | Intended Use Case |
| :--- | :--- | :--- | :--- |
| **Level 0** | `LEVEL_0_QUICK` | Single repetition, small buffers (~500ms total) | Routine revalidation, startup verification. |
| **Level 1** | `LEVEL_1_STANDARD` | 3 repetitions, moderate buffers (~2s–3s total) | Baseline calibration post-install/update. |
| **Level 2** | `LEVEL_2_EXTENDED` | 5 repetitions, larger buffers (~5s–8s total) | In-depth stability & variance analysis. |
| **Level 3** | `LEVEL_3_DIAGNOSTIC` | Full diagnostics, deep repeat cycles | Explicit user troubleshooting. |

---

## 4. Benchmark Execution Phases

A complete benchmark run progresses through eight isolated phases:

```
[1. Environment Verification]
             │
             ▼
      [2. CPU Phase] ────────► (Hashing & integer arithmetic)
             │
             ▼
      [3. RAM Phase] ────────► (Bounded allocation, memory fill & read)
             │
             ▼
      [4. GPU Phase] ────────► (Optional; reports UNAVAILABLE if absent)
             │
             ▼
    [5. Storage Phase] ──────► (Scratch file sequential read/write)
             │
             ▼
 [6. Resource Observation] ──► (Memory pressure & storage delta checks)
             │
             ▼
  [7. Statistical Analysis] ──► (Mean, median, stdev, CV, stability)
             │
             ▼
 [8. Resource Profile Synth] ─► (Derive ResourceCapabilityProfile)
```

A failure in an optional or secondary phase (e.g., GPU compute framework absent) does not fail the overall benchmark.

---

## 5. Statistical Stability & Variation Analysis

To distinguish consistent performance from thermal throttling or background interference, multi-repetition runs compute statistical variance:

- **Coefficient of Variation (CV)**: $\text{CV} = \frac{\sigma}{\mu}$
- **Stability Classification**:
  - `STABLE`: $\text{CV} \le 0.08$ (variance within 8%).
  - `VARIABLE`: $0.08 < \text{CV} \le 0.20$ (moderate environmental variance).
  - `CONSTRAINED`: $\text{CV} > 0.20$ or memory pressure forced workload downscaling.
  - `INCONCLUSIVE`: Aborted or incomplete repetitions.

---

## 6. Hardware Profile State Transition

The benchmark subsystem is the authoritative transition engine for the canonical `HardwareProfile.status`:

```
   [Initial Detection]
            │
            ▼
 [REQUIRES_REBENCHMARK]
            │
            │ (Execute Standard Benchmark)
            ▼
     ┌──────────────┐
     │ Successful?  │
     └──────┬───────┘
            ├──► YES ──► [VALID] (Records benchmark_version, timestamp)
            │
            └──► NO  ──► [INVALID] / [REQUIRES_REBENCHMARK]
```

- **Transition to `VALID`**: Benchmark status is `SUCCESS`, `PARTIAL`, or `CONSTRAINED`. `last_benchmarked_at` and `benchmark_version` are updated; `invalidation_reason` is cleared.
- **Transition to `STALE`**: Triggered externally when profile age exceeds retention policy (e.g., 30 days) or major OS/driver update occurs.
- **Transition to `INVALID`**: Triggered if benchmark fails execution or detects critical hardware corruption.

---

## 7. Derived Resource Capability Profile

Rather than computing an arbitrary single score, the subsystem synthesizes raw measurements into a structured `ResourceCapabilityProfile`:

- **CPU Capacity**: `LOW`, `MODERATE`, `HIGH`, `ULTRA`
- **RAM Capacity**: `CONSTRAINED`, `MODERATE`, `GENEROUS`
- **GPU Compute**: `UNAVAILABLE`, `INTEGRATED`, `ENTRY_DISCRETE`, `MID_DISCRETE`, `HIGH_DISCRETE`
- **GPU VRAM**: `UNAVAILABLE`, `LOW_VRAM`, `MEDIUM_VRAM`, `HIGH_VRAM`
- **Storage Throughput**: `STANDARD`, `FAST_NVME`, `HIGH_IOPS`
- **Storage Latency**: `SUB_MILLISECOND`, `MILLISECOND_SCALE`, `HIGH_LATENCY`
- **Concurrency Capacity**: Bounded integer (1 to 4+ worker slots).

This derived profile provides the factual inputs for future Phase 3 model provisioning.

---

## 8. Lifecycle & Startup Integration

> **Rule**: AEGIS does **NOT** run automatic benchmarks on every application startup.

The benchmark subsystem is invoked strictly according to profile lifecycle state:
1. **First Launch**: Detect hardware -> Run standard benchmark -> Save validated profile.
2. **Normal Startup**: Load cached `VALID` profile (< 1ms).
3. **Re-benchmark Trigger**:
   - Detection detects hardware changes (swapped GPU, altered RAM).
   - Driver or runtime environment materially changes.
   - Benchmark schema version incremented.
   - Profile marked `STALE` by age policy.
   - Explicit user diagnostic request.
