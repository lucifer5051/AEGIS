# AEGIS Fast Path & Fast Decision Architecture

**Document Version**: 1.0.0  
**Status**: ACTIVE / ARCHITECTURAL EXTENSION  
**Author**: AEGIS Architecture & Core Foundation Team  
**Scope**: Low-Latency Execution Routing, Fast Decision Engine Abstraction, and Escalation Rules  
**Canonical Repository**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. Purpose and Philosophy

The **Fast Path** provides the lowest-latency execution route through AEGIS. In a traditional agentic system, every user interaction—even trivial greetings, status queries, or well-defined tool invocations—is passed through a large, slow language model loop. This incurs severe latency (1.5s to 10s+), wastes token compute, and degrades responsiveness.

The Fast Path eliminates this overhead by intercepting requests at the boundary and evaluating whether they can be resolved deterministically, answered from cache, matched against known workflows, or routed via a bounded **Fast Decision Engine** without engaging the full Manager reasoning loop.

---

## 2. The Hot-Path Pipeline

```
                              [USER INPUT]
                                   │
                                   ▼
                       [1. INPUT NORMALIZATION]
                       (Trim, Sanitize, Detect)
                                   │
                                   ▼
                       [2. DETERMINISTIC RULES] ───────► (Matched? ──► Execute Direct)
                                   │
                                   ▼
                       [3. CACHED STATE CHECK] ────────► (Hit? ──────► Return Cached)
                                   │
                                   ▼
                       [4. KNOWN WORKFLOW MAP] ────────► (Matched? ──► Execute Graph)
                                   │
                                   ▼
                       [5. FAST DECISION ENGINE]
                     (Bounded Classification/Route)
                                   │
                                   ▼
                       [6. ESCALATION EVALUATOR]
            ┌──────────────────────┼──────────────────────┐
            ▼                      ▼                      ▼
     [High Confidence]      [Medium Conf]          [Low Conf / Complex]
     (Direct Skill Exec)    (Policy Check)         (Escalate to Manager)
            │                      │                      │
            └──────────────┬───────┘                      │
                           ▼                              ▼
                    [7. TRUST CORE]              [8. MANAGER REASONING]
                     (Authorization)             (Decomposition & Plan)
                           │                              │
                           ▼                              ▼
                      [TOOL EXEC]                    [SKILL PIPELINE]
                           │                              │
                           └──────────────┬───────────────┘
                                          ▼
                                 [9. VERIFICATION]
                                          │
                                          ▼
                                    [USER OUTPUT]
```

---

## 3. Fast Decision Engine Abstraction

The **Fast Decision Engine** is a first-class architectural abstraction responsible for lightweight classification, triage, and routing.

### 3.1 Core Responsibilities

The Fast Decision Engine answers bounded, multiple-choice or parametric classification questions:
1. **Skill Affinity**: Which skill domain is relevant to this user input?
2. **Complexity Assessment**: Is this request trivial, moderate, or complex?
3. **Manager Necessity**: Does this task require multi-step planning or can it run via a single skill invocation?
4. **Memory Dependency**: Does this task require historical memory retrieval, or is it self-contained?
5. **Parallelizability**: Are sub-operations cleanly decouplable?
6. **Workflow Match**: Does this match a pre-registered parametric workflow?
7. **Clarification Trigger**: Is the user's intent too ambiguous to proceed safely?
8. **Execution Locality**: Can this request be satisfied completely locally, or is external egress indicated?
9. **Background Eligibility**: Can this job be queued asynchronously without blocking the user?

### 3.2 Non-Responsibilities & Security Boundaries

The Fast Decision Engine is strictly an **advisory routing classifier**. It must **NEVER**:
- Authorize capabilities or actions.
- Grant file, network, or process permissions.
- Inspect, hold, or inject security credentials or tokens.
- Bypass Trust Core security gates.
- Execute destructive, state-modifying, or egress operations.
- Generate open-ended, unconstrained long-form plans.
- Substitute for Manager reasoning on complex or ambiguous tasks.

---

## 4. Structured Fast Decision Output

To ensure deterministic handling, the Fast Decision Engine never emits unstructured conversational prose. It produces a strictly typed decision record:

```json
{
  "decision_id": "dec-7f39b2e0-4a81",
  "decision_type": "ROUTE_DISPATCH",
  "selected_option": "SKILL_DIRECT",
  "target_component": "system_info_skill",
  "confidence": 0.96,
  "alternatives": [
    {
      "option": "MANAGER_SYNTHESIS",
      "confidence": 0.04
    }
  ],
  "reason_code": "EXACT_INTENT_CAPABILITY_MATCH",
  "latency_ms": 12.4,
  "policy_flags": {
    "requires_memory": false,
    "is_read_only": true,
    "offline_compatible": true,
    "background_eligible": false
  }
}
```

### 4.1 Confidence is Advisory

> [!IMPORTANT]
> **Confidence != Authorization.**  
> A confidence score of `1.0` from the Fast Decision Engine only indicates certainty of classification. It does not confer authority to bypass Trust Core, skip confirmation for Tier 0/1/2 operations, or weaken parameter validation.

---

## 5. Decision Escalation Matrix

The routing subsystem evaluates Fast Decision output against complexity and risk policies to determine the execution path:

| Confidence Level | Task Complexity | Action Risk Level | Routing Outcome | Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **High** (≥ 0.90) | Low / Single-step | Read-Only / Safe | **Fast Path (Direct Skill)** | High certainty, minimal risk; skip Manager reasoning. |
| **High** (≥ 0.90) | Low / Single-step | State-Modifying | **Fast Path -> Trust Core** | Route direct to skill, but Trust Core enforces approval. |
| **Medium** (0.70–0.89) | Low / Single-step | Safe | **Fast Path (Policy-Gated)** | Executed only if explicit policy permits; else Manager. |
| **Medium** (0.70–0.89) | Any | Medium / High Risk | **Escalate to Manager** | Inherent ambiguity requires deliberative Supervisor reasoning. |
| **Low** (< 0.70) | Any | Any | **Escalate to Manager** | Insufficient confidence; Manager must evaluate. |
| **Any** | Complex / Multi-step | Any | **Escalate to Manager** | Requires task decomposition and DAG scheduling. |
| **Any** | High Risk / Destructive | Any | **Trust Core Escalation** | Full verification, policy evaluation, and user confirmation. |
| **Unrecognized** | Unknown | Any | **Clarification Request** | Agent asks user for explicit intent before acting. |

*Note: The numerical thresholds above represent architectural baselines. Concrete thresholds will be calibrated during the Hardware Benchmark Subsystem (Prompt 010).*

---

## 6. Implementation Providers & Offline Compatibility

The Fast Decision Engine is designed with a provider interface allowing interchangeable implementations without altering client code:

```
                  +---------------------------+
                  |    FastDecisionEngine     |  (Architectural Interface)
                  +---------------------------+
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
  [Deterministic]         [Local Compact]         [Remote Provider]
   (Regex / Rules)      (Quantized Classifier)       (Optional)
```

1. **Deterministic Rule Provider**: Sub-millisecond keyword, regex, and intent maps. 100% offline, zero compute cost.
2. **Local Compact Provider**: Small, highly optimized edge classifier running locally on CPU/GPU.
3. **Optional Remote Provider**: Optional remote classifier for hybrid configurations (strictly opt-in).

### 6.1 Strict Offline Fallback

If network connectivity is disabled (`NetworkMode.LOCAL_ONLY`) or offline:
- Remote decision providers are instantly unmapped.
- The engine operates strictly across local compact models and deterministic rule tables.
- Zero functional regression: Any classification that cannot be performed locally falls back cleanly to the local Manager.

---

## 7. Fast Path Invariants

1. **No Silent Mutations**: Every state-modifying action initiated via the Fast Path passes through Trust Core authorization.
2. **Audit Parity**: Fast Path executions generate the same structured audit records as Manager-orchestrated workflows.
3. **Traceability**: Every Fast Path decision includes its `decision_id`, `reason_code`, and evaluation latency in the performance telemetry trail.
