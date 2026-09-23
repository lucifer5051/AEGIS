# AEGIS — Changelog

All notable implementation milestones are recorded here.  
Entries follow **[Prompt ID] — YYYY-MM-DD** ordering, newest first.

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