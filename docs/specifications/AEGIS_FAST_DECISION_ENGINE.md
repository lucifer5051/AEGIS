# AEGIS Fast Decision Engine Specification

**Document Version**: 1.0.0  
**Status**: APPROVED / ACTIVE SPECIFICATION  
**Author**: AEGIS Architecture & Core Foundation Team  
**Scope**: Provider-Neutral Fast Decision Abstraction, Von Option-Marker Provider, and Local Checkpoint Verification  
**Canonical Repository**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. Executive Summary & Architectural Role

The **Fast Decision Engine** is a first-class architectural component of the AEGIS Fast Path (`docs/architecture/AEGIS_FAST_PATH_ARCHITECTURE.md`). In resource-bounded local environments (such as 6 GB VRAM and 16 GB RAM), invoking the full Manager reasoning loop for simple triage or domain routing incurs excessive latency (1.5s to 10s+) and wastes tokens.

The Fast Decision Engine performs bounded, non-autoregressive classification over incoming user requests in sub-50ms intervals. It emits strictly typed, advisory routing decisions that triage requests to specific intent domains without conversational token generation.

```
                              [USER INPUT]
                                   │
                                   ▼
                       [INPUT NORMALIZATION]
                                   │
                                   ▼
                       [TOKEN BUDGET CHECK] ──► (Exceeded? ──► FALLBACK_TO_MANAGER)
                                   │
                                   ▼
                      [FAST DECISION ENGINE]
                     (Non-Blocking Admission)
                                   │
                                   ▼
                    [QUALIFICATION THRESHOLD] ─► (Below min? ─► FALLBACK_TO_MANAGER)
                                   │
                                   ▼
                       [ADVISORY INTENT ROUTE]
                                   │
                                   ▼
                       [TRUST CORE GATEWAY]
                     (Deterministic Authority)
```

---

## 2. Inviolable Security & Architectural Invariants

1. **Advisory-Only Invariant (`is_advisory_only: Literal[True] = True`)**:
   Fast Decision Engine results are strictly advisory intent classifications. Confidence is not authority (`confidence != authorization`). Fast Decision Engine can **NEVER**:
   - Authorize an action or tool execution.
   - Bypass Trust Core security gates.
   - Grant system, network, or file capabilities.
   - Hold or inject credentials or API tokens.
2. **Intent Route vs. Capability Separation**:
   Routes represent user intent classifications (e.g. `FILE_INTENT`, `TERMINAL_INTENT`), completely decoupled from tool selection and execution authority. An identification of `TERMINAL_INTENT` does not authorize terminal execution.
3. **Immutable Trusted Criteria**:
   Decision criteria and choice descriptions are frozen in AEGIS source configuration (`CANONICAL_INTENT_CRITERIA`). Untrusted user input can never alter the routing schema or choice targets.
4. **Hard Token Budget Ceiling (2048 Tokens)**:
   The maximum packed sequence budget is hard-capped at 2048 tokens (`AEGIS_FAST_PATH_MAX_BUDGET`). Requests can request lower budgets, never higher. Inputs exceeding the budget return `FALLBACK_TO_MANAGER` immediately without silent truncation.
5. **Offline by Construction**:
   Inference is strictly local and offline. The engine never binds HTTP ports, runs servers (`von serve`), contacts Hugging Face, or issues network calls during inference.
6. **Zero-Secret Telemetry Invariant**:
   Telemetry logs and exception trails capture timestamps, latencies, route IDs, and confidence metrics. User prompt texts, credentials, and sensitive content are strictly prohibited from telemetry streams.
7. **Fail-Closed to Manager**:
   Any provider failure, timeout, resource pressure, budget breach, or low confidence fails closed to `DecisionOutcome.FALLBACK_TO_MANAGER`.

---

## 3. Provider Abstraction Interface

The provider subsystem defines a provider-neutral lifecycle in `src/decision/providers/base.py`:

```python
class FastDecisionProvider(ABC):
    @abstractmethod
    def initialize(self) -> bool: ...
    @abstractmethod
    def is_ready(self) -> bool: ...
    @abstractmethod
    def evaluate(self, request: FastDecisionRequest, criteria: Dict[DecisionRoute, str]) -> FastDecisionResult: ...
    @abstractmethod
    def unload(self) -> None: ...
    @abstractmethod
    def get_metrics(self) -> ProviderMetrics: ...
    @abstractmethod
    def verify_integrity(self) -> bool: ...
    @abstractmethod
    def qualify(self, hardware_profile: Optional[Any] = None) -> bool: ...
```

---

## 4. Von Provider & Option-Marker Integration

The initial local provider integrates **Von** (`wfzyx/von`), a 395M-parameter non-autoregressive decision model utilizing an Option-Marker joint attention architecture.

### 4.1 Commit & Model Revision Pinning

To prevent configuration drift, both source code and model weights are strictly pinned:
- **Audited Source Commit**: `bed7e7337791c5124a557eb59ad918cb6747b276`
- **Model Revision**: Hugging Face commit `4b2d56a297e59b8a07f0f6ea312389d020d18ceb`
- **Model Name**: `von-1.1.0` (Option-Marker 395M)

### 4.2 Trusted Source-Controlled Manifest

Authoritative artifact checksums are tracked directly in the AEGIS Git repository:
- `schemas/model-manifests/von-1.1-option-marker-395m.json`

Every required checkpoint file must match its expected SHA-256 digest:
| Artifact | Description | Verification Policy |
| :--- | :--- | :--- |
| `config.json` | Model transformer configuration | SHA-256 exact match |
| `tokenizer.json` | Tokenizer vocabulary and merges | SHA-256 exact match |
| `tokenizer_config.json`| Tokenizer settings and special tokens | SHA-256 exact match |
| `special_tokens_map.json`| Special token definitions | SHA-256 exact match |
| `model.safetensors` | Base 395M transformer weights | SHA-256 exact match |
| `option_marker.pt` | Option-Marker joint attention head weights | SHA-256 exact match |

If any file is missing or its SHA-256 digest differs from the trusted manifest, the provider transitions to `DISABLED` and returns fallback.

### 4.3 Storage Location Boundary Rules

The model checkpoint must reside in an isolated AEGIS model directory:
- `%USERPROFILE%\.aegis\models\fast-decision\von\<version>\`

Boundary enforcement strictly forbids:
1. `C:\AEGIS` (recovery copy).
2. The active Git repository.
3. Microsoft OneDrive paths (prevents sync locking and I/O degradation).

---

## 5. Non-Blocking Concurrency Admission

Under bounded host resources, queuing interactive requests behind an in-flight model inference causes high tail latency. The Fast Decision Engine enforces **non-blocking single-flight admission**:

```python
acquired = self._inference_lock.acquire(blocking=False)
if not acquired:
    return self._build_fallback_result(
        route=DecisionRoute.UNKNOWN_INTENT,
        reason=FallbackReason.ADMISSION_BUSY,
    )
```

If inference compute is occupied, subsequent requests immediately fall back to the Manager rather than waiting in a queue.

---

## 6. Route Taxonomy & Calibration Drift Mitigation

### 6.1 Intent Route Taxonomy

| Route Identifier | Semantic Intent Description |
| :--- | :--- |
| `BROWSER_INTENT` | Web search, external documentation lookup, browser viewing. |
| `FILE_INTENT` | Reading, writing, searching local workspace files and directories. |
| `TERMINAL_INTENT` | Executing terminal commands, shell scripts, test runs. |
| `MEMORY_INTENT` | Recalling user facts, project context, past conversation notes. |
| `COMMUNICATION_INTENT` | Drafting messages, sending alerts across channels. |
| `SETTINGS_INTENT` | Modifying configuration, log levels, runtime policies. |
| `REASONING_INTENT` | Multi-step planning, analysis, open-ended problem solving. |
| `UNKNOWN_INTENT` | Ambiguous, unrecognized, or low-confidence inputs. |

### 6.2 Route-Specific Acceptance Thresholds

Because calibration can drift across domains, confidence is evaluated against qualification-calibrated thresholds rather than a monolithic hardcoded number:

| Route Identifier | Qualified Threshold |
| :--- | :--- |
| `BROWSER_INTENT` | ≥ 0.75 |
| `FILE_INTENT` | ≥ 0.75 |
| `TERMINAL_INTENT` | ≥ 0.80 |
| `MEMORY_INTENT` | ≥ 0.75 |
| `COMMUNICATION_INTENT` | ≥ 0.80 |
| `SETTINGS_INTENT` | ≥ 0.80 |
| `REASONING_INTENT` | ≥ 0.85 |
| `UNKNOWN_INTENT` | Always falls back |

If model confidence is below the route's threshold, the engine emits `FALLBACK_TO_MANAGER` with `FallbackReason.LOW_CONFIDENCE`.

---

## 7. Dual Evaluation Datasets & Qualification Protocol

Provider qualification requires passing two decoupled datasets before transitioning to `READY`:

1. **Frozen Qualification Set (`GOLDEN_ROUTING_QUALIFICATION_SET`)**:
   A standard, frozen benchmark of diverse real-world intent queries across all eight routes. Minimum required macro accuracy is **80%**.
2. **Adversarial / Edge Set (`ADVERSARIAL_EDGE_TEST_SET`)**:
   Adversarial inputs testing security boundaries:
   - Prompt injection attempts ("SYSTEM OVERRIDE: ignore instructions...").
   - Capability authorization spoofing ("Authorize root access...").
   - Mixed multi-hop intent queries.
   - Misleading keyword phrasings.
   **100%** of adversarial test cases must correctly trigger `FALLBACK_TO_MANAGER`.

### 7.1 Measured Hardware Capability Gating

Readiness is evaluated against measured host memory and latency benchmarks via `qualify(hardware_profile)`. If the measured load exceeds host available RAM or if latency exceeds threshold, the provider is kept `DISABLED`.

---

## 8. Fallback Escalation Matrix

| Trigger Condition | Decision Outcome | Structured Fallback Reason |
| :--- | :--- | :--- |
| Token estimate > effective budget | `FALLBACK_TO_MANAGER` | `TOKEN_BUDGET_EXCEEDED` |
| Provider uninitialized / disabled | `FALLBACK_TO_MANAGER` | `PROVIDER_UNAVAILABLE` |
| Checkpoint file missing / corrupt | `FALLBACK_TO_MANAGER` | `CHECKSUM_FAILURE` |
| Hardware memory insufficient | `FALLBACK_TO_MANAGER` | `HARDWARE_BUDGET_EXCEEDED` |
| Concurrency lock occupied | `FALLBACK_TO_MANAGER` | `ADMISSION_BUSY` |
| Confidence < qualified threshold | `FALLBACK_TO_MANAGER` | `LOW_CONFIDENCE` |
| Route identified as `UNKNOWN_INTENT` | `FALLBACK_TO_MANAGER` | `LOW_CONFIDENCE` |
| Unhandled backend exception | `FALLBACK_TO_MANAGER` | `INFERENCE_EXCEPTION` |
