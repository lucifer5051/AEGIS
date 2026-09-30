# AEGIS — Changelog

All notable implementation milestones are recorded here.  
Entries follow **[Prompt ID] — YYYY-MM-DD** ordering, newest first.

## [Prompt 017] — 2026-10-01

### Model Manifest Reconciliation & Provisioning Gate (Outcome B: Safe Stop)

**Status:** SAFE STOP — Manifest fully reconciled; live model loading deferred (host RAM headroom insufficient)  
**Tests:** 220 passed · 0 failed · 0 skipped (100 % offline inside AEGIS venv, 17.05s)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (52 files)  
**Type Check:** `mypy` — 0 issues (50 source files)

#### Audit Findings — Root Cause of Manifest Discrepancies

The original manifest `schemas/model-manifests/von-1.1-option-marker-395m.json` contained three categories of errors:

1. **Stale revision**: `model_revision` was `4b2d56a297e59b8a07f0f6ea312389d020d18ceb` — this commit does **not** exist on Hugging Face (`HTTP 404`).
2. **Non-existent artifact**: `special_tokens_map.json` was listed as `required: true`, but this file does not exist in `wfzyx/von` at any audited commit (44 commits audited). Confirmed offline-compatible via `AutoTokenizer.from_pretrained` with `local_files_only=True`.
3. **Stale SHA-256 digests**: All five SHA-256 digests were incorrect against the reconciled revision.

#### Reconciled Revision: `308feac9c53d4080df31b7d81362bffbd086de2d`

| Selection Criterion | Value |
|---|---|
| HF commit | `308feac9c53d4080df31b7d81362bffbd086de2d` |
| HF commit message | `feat(model): release converged Option-Marker Universal weights` |
| Push timestamp | 2026-09-21T20:17Z |
| Paired GitHub source commit | `bed7e7337791c5124a557eb59ad918cb6747b276` (2026-09-20T13:38Z) |

This is the earliest HF commit containing all 5 required artifacts at sizes **exactly matching** the byte counts declared in `von_runtime.py` and `AEGIS_MODEL_RESOURCE_POLICY.md`.

Later commits are incompatible:
- Commits from `3896038655` onward alter `model.safetensors` size (~1.579 GB, different parameter dimensions).
- Commits from `5df8185a4f` onward introduce architecture `1.2.0` (`independent_options` field), breaking the `1.1.0` Option-Marker specification.

Artifact sizes verified stable from commit `308feac9c5` through `272b292a05` (7 commits, inclusive).

#### Reconciled SHA-256 Digests (from LFS pointer records at `308feac9c5`)

| Artifact | Size (bytes) | SHA-256 |
|---|---|---|
| `config.json` | 6,349 | `968db6072229e54691be4d535487a5c8d8132cc4893b6a3fdfa1e605457a7ec3` |
| `tokenizer.json` | 3,583,485 | `3703d011b3ee259e00b59b60772f8fd54998d79e8fa5a1388d507eff28f8f09e` |
| `tokenizer_config.json` | 599 | `57170b72a72c60407eec1f7894000c567309a8e41c5d0504b946c624cb069984` |
| `model.safetensors` | 1,583,355,740 | `349c9b1bf3d5e3217a78c8039606e07069ffae4383640998f99a96bdb6b90c8d` |
| `option_marker.pt` | 1,581,316,050 | `52202f176080d1efe38ca62c45264894fc7e444d332f4af6bac86f4e3146e0f2` |

**Note**: Hashes sourced from authoritative Git LFS pointer blobs. Full on-disk SHA-256 recalculation is required **after download**, before model activation. Integrity gate in `VonFastDecisionProvider.verify_integrity()` is unchanged and mandatory.

#### Files Changed

- **`schemas/model-manifests/von-1.1-option-marker-395m.json`**:
  - `manifest_version`: `1.0.0` → `1.1.0` (reflects reconciliation)
  - `model_revision`: `4b2d56a297...` → `308feac9c53d4080df31b7d81362bffbd086de2d`
  - `required_files`: Removed `special_tokens_map.json`; updated all SHA-256 digests and added `size_bytes` fields
  - Added `reconciliation` block (audit trail, rationale, gate status)
  - Added `compatibility` block (provider references, architecture constraints)
- **`src/decision/providers/von_runtime.py`**:
  - `VON_EXPECTED_SHA256`: Removed `special_tokens_map.json`; updated all 5 SHA-256 values to reconciled hashes
  - Added reconciliation comment block with audit trace
  - `VON_EXACT_ARTIFACT_BYTES` unchanged: still exactly 3,168,262,223 bytes (5 files)
  - `VON_FILES_BY_SIZE` unchanged: file sizes verified stable

#### RAM Gate Decision — Outcome B: Safe Stop

| Metric | Value |
|---|---|
| Available host RAM | 2.27 GB (2,441,334,784 bytes) |
| System RAM reserve floor | 1.50 GB (1,610,612,736 bytes) — **non-negotiable** |
| Available RAM headroom | ~788 MB (830,722,048 bytes) |
| Required for model resident load | ~3.168 GB (3,168,262,223 bytes artifact disk; resident graph estimated ~3.0 GB+) |
| Gate result | **BLOCKED** — headroom insufficient by >2.3 GB |

Model provisioning (download + load) is deferred. The manifest reconciliation is complete and internally consistent. The dynamic memory gate in `SkillConcurrencyManager` and `VonFastDecisionProvider.qualify()` will block admission at runtime until host RAM headroom exceeds 3.0 GB above the 1.5 GB reserve floor.

---

## [Prompt 016] — 2026-09-30

### Phase 3: Hardware Qualification & Model Provisioning (Stop Condition Reached)

**Status:** STOP CONDITION REACHED — Manifest checksum discrepancy & host memory limitation  
**Tests:** 220 passed · 0 failed · 0 skipped (100 % offline inside AEGIS venv)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (52 files)  
**Type Check:** `mypy` — 0 errors (50 source files)

#### Runtime Dependencies Installed (AEGIS venv `%USERPROFILE%\.aegis\venv`)
- `torch 2.6.0+cu124` (isolated in venv, CUDA enabled with NVIDIA RTX 3050 Laptop GPU)
- `transformers 5.18.0`
- `tokenizers 0.23.2`
- `accelerate 1.15.0`
- `safetensors 0.8.0`
- `numpy 2.5.3`
- `von-sdk 1.3.5`
- Zero modification to system Python.

#### Hardware Measurements (Physical Host Live Empirical Inspection)
- Host CPU: Intel Core i5-13450HX (10 cores, 16 logical)
- Total Physical RAM: 15.69 GB (16,849,256,448 bytes)
- Available Host RAM: 2.27 GB (2,441,334,784 bytes)
- Total GPU VRAM: 6.00 GB (6,442,450,944 bytes, 6,144 MiB)
- Free GPU VRAM: 5.49 GB (5,899,288,576 bytes, 5,626 MiB)
- System RAM Reserve Floor: 1.50 GB (1,610,612,736 bytes)
- Available RAM Headroom: 0.77 GB (~788 MB)

#### Model Artifact Verification & Discrepancy (STOP Condition Triggered)
- Inspected canonical manifest: `schemas/model-manifests/von-1.1-option-marker-395m.json`
- Model repo: `wfzyx/von`
- Declared revision: `4b2d56a297e59b8a07f0f6ea312389d020d18ceb`
- Discrepancy 1: Revision `4b2d56a297e59b8a07f0f6ea312389d020d18ceb` does not exist on Hugging Face (`HTTP 404: Not Found`).
- Discrepancy 2: Manifest lists `special_tokens_map.json` (sha256 `9b1287c88b0...`) as required, but this file does not exist in `wfzyx/von`.
- Discrepancy 3: Manifest declares `config.json` sha256 `8a32b988f0cb6733ecbe3db622be147b7486847feecab41d13dbff4d1b8276f7`, but actual commits with 6349 bytes have sha256 `968db6072229e54691be4d535487a5c8d8132cc4893b6a3fdfa1e605457a7ec3`.
- In strict adherence to Instruction #4 ("If any size, revision, source or checksum differs: STOP immediately and report the discrepancy"), no unverified model artifacts were downloaded or loaded into the repository.

#### Concurrency Ceilings Derived from Empirical Measurements
- Lightweight skills (CPU-only, no resident model): 3 concurrent skills (reaches target ceiling with 788 MB headroom / 24 MB marginal).
- Heavyweight model skills (`SKILL_VON_ROUTING`): 0 concurrent skills (model load deferred; available RAM headroom of 788 MB is below the required ~3.0 GB resident footprint, strictly protected by the 1.5 GB system safety reserve).

#### Quality & Type Check Fixes
- `src/model_policy/qualification.py`: Added explicit `Literal["QUALIFIED", "CONSTRAINED", "FAILED"]` type annotation to resolve mypy arg-type error.
- `tests/test_model_policy.py`: Migrated mock method assignments to `monkeypatch.setattr` to resolve mypy method-assign errors.
- Applied `ruff format` across 52 source and test files.

---

## [Prompt 014B] — 2026-09-23  `a44d40b`

### Model Resource Policy & Skill Concurrency Engine

**Commit:** `a44d40b4c03ca086f57dc2edc15f9cfcec2f87d5`  
**Tests:** 220 passed · 0 failed · 0 skipped (100 % offline)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean

#### Added

- **`src/model_policy/`** — new package (8 modules):
  - `models.py` — canonical data models: `ModelArtifact`, `RuntimeSpec`, `SkillResourceProfile`, `SkillExecutionLease`, `AdmissionDecision`, enums `QualificationState` / `AdmissionResult`.
  - `runtime_registry.py` — heterogeneous runtime registry tracking per-runtime identity, package dependencies, disk footprint, and shared-runtime relationships; avoids double-counting runtimes shared by multiple models.
  - `residency_manager.py` — ref-counted in-memory model residency; eviction safety (refuses to evict a model with active leases).
  - `concurrency_manager.py` — `SkillConcurrencyManager`: qualification-derived concurrency ceiling `min(target_skills, 1 + (ram_headroom // marginal_ram))`; per-skill RAM/VRAM reservation tracking; lightweight-skill fast-path; queue/defer/fallback on admission failure.
  - `live_benchmark.py` — `LiveHardwareBenchmark`: empirical RAM/VRAM delta probing (`MEASURED_PROBE_LOAD_AND_LATENCY` protocol).
  - `qualification.py` — `ModelHardwareQualifier`: drives the live probe, stores empirical results, transitions model to `QUALIFICATION_DERIVED` state.
  - `storage_policy.py` — `ModelStoragePolicy`: manifest-pinned artifact sizing, global storage budget (30.0 GB), host reserve floor (40.0 GB), download + extracted + headroom pre-flight calculation.
  - `__init__.py` — clean public API surface.

- **`src/decision/providers/`** — local model provider runtime constants (artifact byte count, digests, RAM/VRAM design estimates, flagged as pending empirical qualification).

- **`src/config/models.py`** — added `system_ram_reserve_mb`, `system_vram_reserve_mb` configurable fields to `ProvisioningPolicyConfig`.

- **`src/decision/providers/`** — integrated `SkillConcurrencyManager` lease acquisition into the Fast Decision Engine provider; non-blocking admission with structured fallback.

- **`docs/specifications/AEGIS_MODEL_RESOURCE_POLICY.md`** — authoritative specification: three-tier hierarchy (Skills → Models → Runtimes), qualification-derived concurrency formula, storage budget arithmetic, qualification protocol.

- **`tests/test_model_policy.py`** — 20 unit tests covering all 8 modules; 100 % offline.

#### Key Architectural Decisions

| Decision | Value |
|---|---|
| Concurrent-skill target | 2–3 skills (not 2–3 resident models) |
| Concurrency ceiling basis | Empirically measured RAM/VRAM headroom |
| System RAM reserve | 1.5 GB (configurable via `ProvisioningPolicyConfig`) |
| System VRAM reserve | 512 MB (configurable) |
| Global model storage budget | 30.0 GB |
| Host storage floor | 40.0 GB |
| Runtime double-counting | Prevented — shared runtimes counted once across models |

> ⚠️ **PENDING — Local Model Runtime Installation & Hardware Qualification**
>
> Model runtime installation, local model artifact download, and live inference
> benchmarking (`MEASURED_PROBE_LOAD_AND_LATENCY`) have **NOT** been performed
> on this machine. Concurrency limits above are design-specified estimates derived
> from architecture. All empirical residency values (resident RAM/VRAM delta,
> marginal per-inference cost) must be measured before concurrency ceilings are
> treated as authoritative. Explicit human approval is required before any
> model runtime installation or artifact download proceeds.

---

## [Prompt 014A] — 2026-09-23  `cafbbb7`

### Fast Decision Engine — Lint & Test Stabilisation

**Commit:** `cafbbb7`  
**Tests:** 200 passed · 0 failed · 0 skipped  
**Lint:** `ruff check` — 0 errors (E501 violations resolved)

- Resolved all line-length (E501) lint violations across `src/decision/`.
- Verified 200-test baseline prior to 014B implementation.

---

## [Earlier Milestones] — 2026-09-20 and prior

### Domain Schema Foundation  `db92220`
- Aligned domain schema with architecture; added EKM, projects, conversations domain models.

### SQLite Storage Foundation  `2e7728b` / `53ce80d`
- Domain schema implementation, SQLite WAL storage subsystem.

### Hardware Benchmark Subsystem  `b0885e5`
- Controlled micro-benchmarking framework for CPU, memory, storage with profile state-machine.

### Hardware Detection Subsystem  `31fa866`
- Read-only `HardwareProfile` detection across CPU, RAM, GPU, storage, OS, runtimes.

### Configuration Subsystem  *(earlier)*
- 5-tier precedence loader, 13-domain Pydantic schemas, Windows-path safety, zero-secret enforcement.

### Repository & Foundation  *(initial)*
- Canonical directory structure, development environment, technical foundation specification.

---

*This changelog records implementation milestones only. Design documents and specifications live in `docs/specifications/`.*