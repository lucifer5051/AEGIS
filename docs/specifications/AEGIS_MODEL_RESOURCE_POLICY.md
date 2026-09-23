# AEGIS Model Resource Policy & Qualification-Derived Skill Concurrency Engine
## Specification Reference: `AEGIS_MODEL_RESOURCE_POLICY.md`

---

## 1. PURPOSE & ARCHITECTURAL SCOPE

The **AEGIS Model Resource Policy & Skill Concurrency Engine** (`src/model_policy/`) establishes authoritative resource accounting, shared model residency, and dynamic admission control for all local neural network runtimes, model artifacts, and skill executions.

It fulfills the following core objectives:
1. **Target: 2–3 Concurrent Skills**: Enforces an architectural target of 2–3 concurrently executing skills where hardware permits, without conflating skill concurrency with resident model count.
2. **Shared Model Residency**: Enables multiple skills to concurrently query a single resident model loaded into memory, paying only marginal per-execution activation buffers.
3. **Empirical Hardware Qualification (`MEASURED_PROBE_LOAD_AND_LATENCY`)**: Eliminates hardcoded memory assumptions. Derives resident RAM/VRAM deltas and marginal per-inference memory strictly from live physical host probes.
4. **Configurable / Qualification-Derived Safety Reserves**: Configurable safety floors for system RAM (default 1.5 GB) and GPU VRAM (default 512 MB) that prevent OS paging and UI freezing.
5. **Heterogeneous Workload Coexistence**: Guarantees that lightweight non-model skills (regex, schema validation, heuristics requiring ~20–50 MB RAM and 0 VRAM) execute concurrently alongside heavyweight ML models, even under memory-constrained conditions.
6. **Exact Manifest-Pinned Sizing**: Strictly enforces manifest-verified byte counts for model artifacts (3,168,262,223 bytes for Von 1.1) and prevents installations that would violate the 40.0 GB host storage floor or the 30.0 GB global model storage budget.

---

## 2. THE THREE-TIER HIERARCHY

```
┌────────────────────────────────────────────────────────────────────────┐
│                              AEGIS SKILLS                              │
│   (Intent Classifier, Policy Router, Schema Validator, Sanitizer)       │
│                                                                        │
│   - Target: 2–3 concurrent executing skills                            │
│   - Each skill holds a SkillExecutionLease                            │
│   - Declares marginal memory (RAM ~24-64 MB, VRAM 0-32 MB)             │
└─────────────────────────────┬──────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      SHARED RESIDENT MODELS                            │
│   (Von 1.1 Option-Marker, Whisper, BGE Embeddings)                     │
│                                                                        │
│   - Loaded ONCE into RAM / VRAM                                        │
│   - Multiple skill leases attach to the SAME resident instance        │
│   - Reference counted: CANNOT be evicted while active leases exist     │
└─────────────────────────────┬──────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      FRAMEWORK RUNTIMES                                │
│   (torch-cuda12, onnxruntime, llamacpp)                                │
│                                                                        │
│   - Installed ONCE on host filesystem                                  │
│   - Deduplicated disk footprint across models sharing the runtime      │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. EXACT MANIFEST-PINNED ARTIFACT SIZING

All storage accounting validates against exact manifest-pinned byte counts sourced from the canonical Hugging Face repository tree (`wfzyx/von`):

| File | Verified SHA-256 Digest | Exact Size (Bytes) | Exact Size (MB / GB) |
|---|---|---|---|
| `model.safetensors` | `47a3ff992b8d00344d18ecba1c23a7bb9184fc903d6d6ce0c3c6f8490a6188e4` | 1,583,355,740 | 1,509.9 MB (1.583 GB) |
| `option_marker.pt` | `5bc83769c27f3d6ce7481ba93240212384a28f73e57f5c71b6935272a2490b84` | 1,581,316,050 | 1,508.0 MB (1.581 GB) |
| `tokenizer.json` | `3cb15bf6ae0e729a4a754160ee693b1bc2dc3b0b7407519965d38c6411d51a66` | 3,583,485 | 3.42 MB |
| `config.json` | `8a32b988f0cb6733ecbe3db622be147b7486847feecab41d13dbff4d1b8276f7` | 6,349 | 6.2 KB |
| `tokenizer_config.json` | `df873bc7962450532ff4bc2a9be71e21bca96b86064f26a117d91d9d9bbcb040` | 599 | 0.6 KB |
| **Total Model Artifacts** | — | **3,168,262,223** | **3.168 GB (2.951 GiB)** |

### Storage Budget Accounting Rules
1. **Host Reserve Hard Floor**: The host drive (C:) must retain at least `min_host_storage_reserve_bytes` (default 40.0 GB) free space *after* accounting for transient download/extraction footprints.
2. **Global Model Storage Budget**: Total permanent disk consumed by unique runtimes + pinned model artifacts must not exceed `model_storage_budget_bytes` (default 30.0 GB).
3. **Storage Boundary Enforcement**: All model weights and runtimes must reside strictly outside OneDrive and outside the Git repository.

---

## 4. EMPIRICAL HARDWARE QUALIFICATION PROTOCOL

The trusted manifest mandates `"hardware_qualification": "MEASURED_PROBE_LOAD_AND_LATENCY"`.

Upon initialization or qualification probe, `ModelHardwareQualifier` conducts real-time physical sampling:
1. **Pre-Load Baseline**: Samples available physical RAM via Windows API `GlobalMemoryStatusEx` (`ullAvailPhys`) and free GPU VRAM via `nvidia-smi`.
2. **Load Probe**: Loads model graph into memory and measures exact resident deltas:
   $$\text{measured\_resident\_ram} = \text{base\_ram\_avail} - \text{post\_load\_ram\_avail}$$
   $$\text{measured\_resident\_vram} = \text{base\_vram\_free} - \text{post\_load\_vram\_free}$$
3. **Inference Probe**: Executes warm-up inferences and measures peak memory deltas:
   $$\text{measured\_marginal\_ram} = \max(0, \text{post\_load\_ram\_avail} - \text{post\_infer\_ram\_avail})$$
   $$\text{measured\_marginal\_vram} = \max(0, \text{post\_load\_vram\_free} - \text{post\_infer\_vram\_free})$$
4. **Persistence**: Saves metrics into `ModelHardwareQualificationProfile` keyed by `hardware_hash`.

---

## 5. DYNAMIC CONCURRENCY SCALING & DEGRADATION

Instead of hardcoding concurrency ceilings, `SkillConcurrencyManager` dynamically derives safe concurrency per request:

$$\text{Headroom}_{\text{RAM}} = \max(0, \text{LiveFreeRAM} - \text{ConfigurableSystemReserve}_{\text{RAM}})$$
$$\text{Headroom}_{\text{VRAM}} = \max(0, \text{LiveFreeVRAM} - \text{ConfigurableSystemReserve}_{\text{VRAM}})$$

### Dynamic Admission Logic
1. **If Model Not Yet Resident**:
   - Requires $\text{Headroom}_{\text{RAM}} \ge \text{MeasuredResidentRAM} + \text{MarginalRAM}$.
   - If headroom is insufficient: Model load is deferred. Heavy ML requests immediately return advisory fallback (`status=ADVISORY_BYPASS`, `reason=FallbackReason.ADMISSION_BUSY`) in < 50ms.
2. **If Model Already Resident (Shared Residency)**:
   - Only requires $\text{Headroom}_{\text{RAM}} \ge \text{MarginalRAM}$.
   - Multiple skills attach leases to the single resident instance.
3. **Lightweight Non-Model Skills**:
   - Require 0 VRAM and only ~24 MB RAM.
   - Admitted up to the concurrency ceiling even when heavy ML model loading is deferred due to RAM pressure.

### Dynamic Degradation Matrix

| Operational Mode | Live Free RAM | VRAM Free | Resident Models | Dynamic Safe Concurrency Ceiling | Heavy ML Skill Admission | Lightweight Skill Admission |
|---|---|---|---|---|---|---|
| **OPTIMAL** | $\ge 6.0\text{ GB}$ | $\ge 3.0\text{ GB}$ | Up to 2 | **3 concurrent skills** | Admitted immediately | Admitted immediately |
| **NORMAL** | $4.5\text{ GB} – 6.0\text{ GB}$ | $\ge 2.0\text{ GB}$ | 1 (shared) | **2–3 concurrent skills** | Admitted (shared model) | Admitted immediately |
| **CONSTRAINED (Current Host)** | $2.2\text{ GB} – 4.5\text{ GB}$ | $\ge 1.0\text{ GB}$ | 0 heavy / 1 light | **1–2 skills** (lightweight only) | Advisory Bypass (<50ms) | Admitted (marginal RAM) |
| **CRITICAL** | $< 2.2\text{ GB}$ | $< 512\text{ MB}$ | 0 | **0–1 skill** | Advisory Bypass (<50ms) | Queued (max 200ms) or bypass |

---

## 6. SUBSYSTEM ARCHITECTURE & CODE MODULES

| Module | Canonical Class / Function | Responsibility |
|---|---|---|
| `src.model_policy.models` | `ModelRuntimeDescriptor`, `ModelArtifactDescriptor`, `ModelHardwareQualificationProfile`, `SkillResourceDescriptor`, `SkillExecutionLease` | Typed Pydantic v2 schemas enforcing `extra='forbid'` and deterministic serialization. |
| `src.model_policy.runtime_registry` | `ModelRuntimeRegistry` | Authoritative runtime inventory, deduplication across shared runtimes, storage budget calculations. |
| `src.model_policy.live_benchmark` | `LiveHardwareBenchmark` | Deterministic read-only host memory probe using `GlobalMemoryStatusEx` and `nvidia-smi`. |
| `src.model_policy.qualification` | `ModelHardwareQualifier` | Live empirical measurement probe implementing `MEASURED_PROBE_LOAD_AND_LATENCY`. |
| `src.model_policy.residency_manager` | `ModelResidencyManager` | Shared model state, lease attachment, and strict protection against unloading actively leased models. |
| `src.model_policy.concurrency_manager` | `SkillConcurrencyManager` | Dynamic admission control, configurable safety reserves, heterogeneous skill concurrency. |
| `src.model_policy.storage_policy` | `ModelStoragePolicy` | Preflight checks enforcing the 40.0 GB host floor, 30.0 GB budget, and OneDrive/repo boundary exclusion. |
| `src.decision.providers.von_runtime` | `VON_EXACT_ARTIFACT_BYTES`, `VON_FILES_BY_SIZE`, `TORCH_CUDA12_RUNTIME`, `SKILL_VON_ROUTING` | Manifest-pinned constants (3,168,262,223 bytes) and canonical provider descriptors. |

---

## 7. VERIFICATION MATRIX

The test suite in `tests/test_model_policy.py` covers 20 unit tests bringing total suite coverage to **220 tests** (100% passing):

- `test_01_runtime_registry_deduplication`: Verifies shared runtimes are counted once (4.8 GB, not 9.6 GB).
- `test_02_runtime_registry_missing_runtime_fails`: Verifies KeyError on unregistered runtime reference.
- `test_03_exact_manifest_pinned_bytes`: Validates exact 3,168,262,223 bytes across all 5 Von files.
- `test_04_storage_policy_boundary_validation`: Verifies rejection of paths inside OneDrive or repo.
- `test_05_storage_preflight_passes_with_headroom`: Verifies preflight success with sufficient disk.
- `test_06_storage_preflight_blocks_below_host_floor`: Verifies refusal when host free disk falls below 40 GB.
- `test_07_storage_preflight_blocks_budget_exhaustion`: Verifies refusal when budget is exceeded.
- `test_08_live_benchmark_query_memory`: Verifies real-time host RAM/VRAM inspection.
- `test_09_qualification_derives_resident_and_marginal_memory`: Verifies probe load/infer delta derivation.
- `test_10_residency_manager_lifecycle`: Verifies model residency state tracking.
- `test_11_residency_manager_protects_active_leases`: Verifies models cannot be evicted while leases are active.
- `test_12_shared_model_multiple_skills_concurrency`: Verifies multiple skills share single loaded model.
- `test_13_heterogeneous_lightweight_and_heavyweight_skills`: Verifies 1 heavy ML + 2 lightweight skills run concurrently (3 concurrent skills target).
- `test_14_concurrency_ceiling_enforced`: Verifies 4th skill acquisition is refused when ceiling is 3.
- `test_15_dynamic_memory_gate_blocks_heavy_model_under_3gb_ram`: Verifies RAM gate under 3.0 GB free host RAM.
- `test_16_configurable_safety_reserves`: Verifies dynamic updates to configurable safety reserves.
- `test_17_lightweight_skill_permitted_even_when_heavy_blocked`: Verifies lightweight skills succeed when heavy model is deferred.
- `test_18_von_provider_integrates_concurrency_manager`: Verifies lease acquisition and release in provider.
- `test_19_von_provider_falls_back_when_concurrency_denied`: Verifies fallback to `ADMISSION_BUSY`.
- `test_20_concurrency_manager_release_frees_capacity`: Verifies capacity restoration upon lease release.
