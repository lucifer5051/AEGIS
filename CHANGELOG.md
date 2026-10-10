# SAGE — Changelog

All notable implementation milestones are recorded here.  
Entries follow **[Prompt ID] — YYYY-MM-DD** ordering, newest first.

## [Prompt 027] — 2026-10-10

### Durable Workflow Engine & Task Scheduler

**Status:** IMPLEMENTED & VERIFIED — Implemented durable workflow engine (`src/workflow/`) and resilient task scheduler (`src/scheduler/`) backed by SQLite schema migration `0007_workflow_scheduler_schema.sql`. Integrates end-to-end with Supervisor, StateStore checkpoints, Tool Broker, and Trust Core authorization. Supports DAG validation, cycle detection, argument interpolation, bounded retries, budget limits, pause/resume/cancellation, one-shot/cron/event schedules, deduplication, and downtime missed-run handling.  
**Tests:** 500 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~45.2s) — 20 new tests (10 in `tests/test_workflow_engine.py`, 10 in `tests/test_scheduler.py`)  
**Lint:** `ruff check src/ tests/` — 0 errors · `ruff format --check src/ tests/` — clean (132 files)  
**Type Check:** `mypy src/ tests/` — 0 issues (130 source files)  

#### A. Durable Workflow Engine (`src/workflow/`)
- `WorkflowDefinition`, `WorkflowStepDefinition`, `WorkflowBudget`, `WorkflowRun`: Typed, immutable data models representing versioned workflow definitions and durable run instances.
- `WorkflowValidator`: Structural checks, DAG cycle detection using 3-color DFS graph traversal, dependency validation, input/output reference interpolation checks (`{{inputs.key}}`, `{{steps.step_id.outputs.key}}`), zero-secret detection, and tool registration validation.
- `WorkflowStore`: SQLite persistence for workflow definitions and runs across application restarts, ensuring ACID state transitions (`PENDING`, `RUNNING`, `PAUSED`, `COMPLETED`, `FAILED`, `CANCELLED`).
- `WorkflowEngine`: Dependency-aware execution loop resolving step topological eligibility; step argument interpolation; execution budgeting and deadline enforcement; bounded retries and failure policy (`FAIL_FAST`, `CONTINUE_ON_FAILURE`, `RETRY`); task-level checkpoint persistence via `StateStore`; idempotency token tracking to avoid duplicate side effects during recovery.
- Verification Pipeline Integration: Executes steps through `ToolBroker` and `TrustCore`; enforces `StepVerifier` policies (`deterministic_check`, `requires_approval`, `trusted_output`); pauses workflows safely on approval escalation or authorization revocation.

#### B. Durable Scheduler (`src/scheduler/`)
- `ScheduleDefinition`, `ScheduleExecutionRecord`: Typed models for one-shot, recurring cron, and event-triggered schedule policies.
- `compute_next_cron_run()`: Standard 5-part cron evaluator (`minute hour day-of-month month day-of-week`) supporting ranges, steps (`*/15`), lists, and wildcards.
- `SchedulerStore`: SQLite persistence with uniqueness constraint and atomic deduplication on `(schedule_id, scheduled_for)` to prevent double executions across poll cycles and system restarts.
- `WorkflowScheduler`: Periodic poller and dispatcher; supports `ONE_SHOT`, `CRON_RECURRING`, and `EVENT_TRIGGERED` actions; handles downtime missed runs according to configured policy (`SKIP`, `RUN_ONCE`, `FAIL_ALERT`); enforces bounded concurrency (`max_concurrent_runs`); supports dynamic pause, resume, and cancellation.

#### C. Migration & Test Environment Compatibility
- `migrations/0007_workflow_scheduler_schema.sql`: Staged migration adding `workflow_definitions`, `workflow_runs`, `schedules`, and `schedule_executions` tables with foreign keys and compound indexes.
- Environment & schema validation tests updated in `tests/test_domain_schema.py` and `tests/test_environment_validation.py` to register migration 0007 and runtime packages.

## [Prompt 026] — 2026-10-10

### Approved Lesson Retrieval & Context IR Integration

**Status:** IMPLEMENTED & VERIFIED — Integrated approved `ExecutionLesson` records into the canonical EKM retrieval pipeline (`src/memory/retriever.py`), bounded Context IR compilation (`src/memory/compiler.py`), and the unified Manager context layer (`src/manager/context.py`, `src/manager/supervisor.py`). Verified 14 comprehensive requirements covering state filtering, scope isolation, deduplication, ranking, budget enforcement, and security non-escalation.  
**Tests:** 480 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~39.7s) — 14 new retrieval and Context IR integration tests (`tests/test_lesson_retrieval_context.py`)  
**Lint:** `ruff check src/ tests/` — 0 errors · `ruff format --check src/ tests/` — clean (118 files)  
**Type Check:** `mypy src/ tests/` — 0 issues (116 source files)  

#### A. Approved-Only Lesson Retrieval Pipeline
- `EKMRetriever._retrieve_lessons()`: Restricts lesson retrieval strictly to `LessonState.APPROVED`. Excludes every non-active state (`CANDIDATE`, `VERIFIED`, `REJECTED`, `INVALIDATED`, `SUPERSEDED`).
- Scope boundary isolation: Unscoped queries (`project_id=None`) only retrieve global lessons; scoped queries retrieve matching project lessons plus global lessons when `include_global=True`. Zero cross-project leakage.
- Skill relevance filtering: When `query.skill_id` is supplied, non-matching lessons are filtered out.
- Deterministic multi-factor ranking: Scores candidate lessons based on task query keyword matching against trigger condition, recommended action, observation, and title (0.0 to 0.40), scope specificity (0.15), evidence corroboration quality (0.0 to 0.25), and recency bonus (0.0 to 0.20).
- Evidence & provenance preservation: Supporting event and evidence IDs are preserved on the lesson record and mapped to `provenance_map` in `RetrievalResult`.

#### B. Context IR Compilation & Deduplication
- `ContextCompiler`: Represents lessons distinctly within `ContextIR` and prompt strings under `## Relevant Verified Lessons (Advisory Experience)`.
- Explicit separation: Clearly demarcates `Observed:` (factual empirical results) from `Explanation:` (inferred theories).
- Deduplication against canonical facts: When an approved lesson was promoted to an EKM fact (`lesson.fact_id`), the duplicate memory fact is suppressed to conserve token budget.
- Strict token budget enforcement: Enforces `available_token_budget` limit, tracking included lessons in `included_lesson_ids` and recording omitted lessons in `omitted_lesson_ids`.
- `ExecutionLessonContext`: Immutable context DTO containing lesson ID, title, trigger condition, recommended action, observation, explanation, risk level, state, evidence IDs, and scope.

#### C. Supervisor Wiring & Security Invariants
- `Supervisor` & `ContextAssembler`: Injects `LessonManager` into context assembly pipeline, surfacing approved lessons in `ContextIR.relevant_lessons` with zero additional LLM or model overhead during retrieval.
- Deterministic Security Boundary: Lessons remain strictly advisory context. Verified via test that lesson records cannot grant tool permissions, bypass Tool Broker policies, alter Trust Core autonomy tiers, or trigger automatic action execution.

## [Prompt 025] — 2026-10-10

### Durable Experience Memory & Evidence-Gated Lesson Lifecycle

**Status:** IMPLEMENTED & VERIFIED — Complete evidence-gated lesson lifecycle (`src/memory/lessons.py`): `ExecutionLesson`, `LessonManager`, `LessonProposal`. Fixed circular import between `src/skills/__init__.py` and `src/memory/lessons`. Fixed invalid `provenance_source="experience"` (DB CHECK violation) to `"system"`.  
**Tests:** 466 passed · 0 failed · 0 skipped (100% offline inside SAGE venv) — 24 new lesson lifecycle tests (`tests/test_lesson_manager.py`)  

#### A. Evidence-Gated Lesson Proposal & Lifecycle
- `LessonManager.propose_candidate()`: Creates `CANDIDATE` lesson from `ExperienceEvent` + `LessonProposal`. Enforces zero-secret invariant on all text fields. A single execution NEVER directly creates an APPROVED lesson.
- `LessonManager.corroborate_lesson()`: Accumulates supporting or contradicting execution telemetry. Applies risk-based promotion thresholds: LOW(2), MEDIUM(3), HIGH(4 or explicit user confirmation).
- Lifecycle states: `CANDIDATE → VERIFIED → APPROVED`, `REJECTED`, `INVALIDATED`, `SUPERSEDED`.
- `invalidate_lesson()` and `supersede_lesson()` cascade to EKM canonical fact invalidation (non-blocking).

#### B. EKM Integration on Promotion
- On promotion to `APPROVED`, `LessonManager` calls `EKMMemoryStore.add_fact()` creating a canonical `memory_facts` record tagged with `operational_lesson`.
- `fact_id` is populated on the lesson record only after confirmed APPROVED promotion.
- EKM store errors during promotion are non-blocking; lesson still transitions to APPROVED.
- `provenance_source` corrected to `"system"` (satisfies DB CHECK constraint).

#### C. Persistence & Scoped Retrieval
- SQLite-backed `execution_lessons` table with indexes on `skill_id`, `project_id`, `state`.
- Dual-path storage: in-memory dict + SQLite `INSERT OR REPLACE` upserts.
- `get_lesson()` returns `None` (never raises) for unknown IDs.
- `list_lessons()` supports filtering by `project_id`, `skill_id`, `states`, and `include_global` for global-scope fallback.
- In-memory mode (no SQLite) fully functional for offline/testing contexts.

#### D. Bug Fixes
- **Circular Import**: Removed `src.memory.lessons` imports from `src/skills/__init__.py`. Lesson types now accessed from `src.memory` directly, breaking `skills → memory → skills` cycle.
- **DB Constraint**: Fixed `provenance_source="experience"` → `"system"` in `LessonManager.corroborate_lesson()`.

## [Prompt 024] — 2026-10-10

### Experience & Knowledge Memory (EKM) Runtime, Selective Retrieval & Context IR

**Status:** IMPLEMENTED & VERIFIED — Full implementation and integration of Experience & Knowledge Memory (EKM) runtime (`src/memory/store.py`), derived FTS5 indexing (`src/memory/indexer.py`), multi-stage selective retrieval pipeline (`src/memory/retriever.py`), bounded Context IR compilation (`src/memory/compiler.py`), and optional semantic backend abstraction (`src/memory/semantic.py`). Fully integrated with `ContextAssembler` and `Supervisor`.  
**Tests:** 442 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~43.5s) — 24 new EKM runtime & retrieval tests (`tests/test_ekm_memory_runtime.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (115 files)  
**Type Check:** `mypy` — 0 issues (95 source files)  

#### A. Authoritative Typed Persistence & Secret Protection
- Created `EKMMemoryStore` managing canonical relational SQLite tables (`memory_facts`, `user_preferences`, `entities`, `entity_relationships`, `memory_evidence`).
- Enforced zero secret leakage via `_check_no_secrets` raising `SecretLeakError` on credentials, API keys, bearer tokens, or private keys across all write paths.
- Preserved historical records via explicit supersession tracking (`supersede_fact`, `supersede_preference`).
- Enforced frozen provenance trust levels M0–M4 (`MemoryTrustLevel`).

#### B. Derived Full-Text Search (FTS5) Indexing
- Implemented `FTS5IndexManager` operating auxiliary virtual table `ekm_fts` (`tokenize='porter unicode61'`).
- Invariant upheld: derived index is 100% rebuildable from authoritative relational tables via `rebuild_index()`.
- Added `sanitize_fts5_query()` preventing malformed punctuation/syntax crashes and expanding camelCase tokens for bidirectional matching.
- Integrated `is_superseded` auxiliary state in FTS5 index with `mark_superseded()`.

#### C. Multi-Stage Selective Retrieval
- Implemented `EKMRetriever` covering:
  1. Scope boundary filtering (`project_id`, global fallback, strict project isolation).
  2. Temporal validity filtering (`valid_from <= as_of_time <= valid_until`, active vs expired, supersession handling).
  3. FTS5 lexical matching combined with entity canonical slug matching.
  4. Entity relationship graph traversal up to `max_relationship_depth`.
  5. Trust-aware ranking combining BM25 score, trust weights (M0=0.2 to M4=1.0), epistemic confidence, importance, and project-specificity bonus.
  6. Deduplication and conflict detection: flags opposing facts and opposing preferences in `unresolved_conflicts`.
  7. Detailed provenance attribution map per returned record.

#### D. Bounded Context IR Compilation & Manager Integration
- Implemented `ContextCompiler` converting retrieval results into bounded `ContextIR` instances within explicit token budgets (`context_token_budget`).
- Integrated deterministic token estimation with uncertainty bounds (±15%) and omission tracking (`omitted_record_ids`).
- Enhanced `ContextAssembler` and `Supervisor` with optional `memory_store` / `retriever` wiring, enabling seamless context assembly for planning while retaining 100% backward compatibility.
- Designed `SemanticMemoryBackend` protocol with `NullSemanticBackend` reporting status `DESIGNED` without network/GPU dependencies.

## [Prompt 023] — 2026-10-09

### End-to-End Skill Execution, Verification & State Recovery Pipeline

**Status:** IMPLEMENTED & VERIFIED — Full integration of User Goal -> ClarificationGate -> Supervisor -> SkillRegistry -> SkillAdmissionController -> BoundSkillToolBroker -> ToolBroker -> TrustCore -> Verifier -> Checkpoint/Reconciliation -> ExperienceEvent pipeline.  
**Tests:** 418 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~43.5s) — 24 new pipeline integration tests (`tests/test_integration_skills_execution.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (107 files)  
**Type Check:** `mypy` — 0 issues (105 source files)  

#### A. Supervisor & Skills Pipeline Integration
- Connected `Supervisor.execute_skill()` to `SkillAdmissionController` with strict lifecycle transitions (`AVAILABLE -> SELECTED -> ADMITTED -> EXECUTING -> VERIFYING -> SUCCEEDED -> AVAILABLE`).
- Wired `Supervisor.select_skill()` to `SkillRegistry` deterministic intent matching.
- Wired `ClarificationGate` short-circuiting on incomplete or empty goals prior to any LLM invocation.
- Exported `GoalExecutionResult` in public `src.manager` API.
- Added `reconcile_on_startup` alias to `ReconciliationEngine` for consistent startup recovery invocation.

#### B. Security & Boundary Guarantees
- Validated that `BoundSkillToolBroker` strictly blocks undeclared tools (`SkillToolAccessDeniedError`).
- Verified that unknown tools not present in `ToolBroker` catalog are rejected (`ToolNotFoundError`).
- Verified that `TrustCore` autonomously denies or escalates unpermitted actions even if declared in skill bindings (`AutonomyTier.STRICT_MANUAL`).
- Ensured zero secret leakage in `ExperienceEvent` payloads and `SkillCheckpointMetadata` persistence (`SecretLeakError`).

#### C. Verification & Recovery Guarantees
- Demonstrated post-tool-execution semantic verification failure (`StepVerifier` catches failure even when tool returns `SUCCESS`).
- Ensured non-idempotent side-effect steps are never automatically retried during recovery.
- Re-authorization via `TrustCore` enforced on every retry step.
- Persisted and reconstructed checkpoint execution state via `ReconciliationEngine`.

#### D. Telemetry & Fast Path
- Verified `ExperienceEvent` classification distinguishing `USER_CORRECTED` from execution failures and `NO_FEEDBACK` from assumed success.
- Verified deterministic fast path for skill execution with zero LLM reasoning invocations.
- Verified 100% offline execution with zero socket/network operations.

## [Prompt 022] — 2026-10-08

### Phase 6: SAGE Skills Architecture & Tool Integration Foundation

**Status:** IMPLEMENTED & VERIFIED — Declarative SkillManifest schema, deterministic manifest validation, fail-closed SkillRegistry, BoundSkillToolBroker, DeterministicSkillExecutor, SkillAdmissionController, ClarificationGate, ExperienceFeedbackService, and SkillCheckpoint boundary.  
**Tests:** 394 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~40.8s) — 20 new Skills subsystem tests (`tests/test_skills.py`), plus approval lifecycle and manager recovery tests.  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (106 files)  
**Type Check:** `mypy` — 0 issues (104 source files)  

#### A. Declarative Skills Architecture (`src/skills/`)
- Implemented `SkillManifest` model with strict SemVer, regex identifier validation, risk classes, and resource declarations.
- Enforced security invariant: Skills cannot define or override Trust Core policies.
- Implemented fail-closed `SkillRegistry` with deterministic lifecycle state machine (`AVAILABLE`, `SELECTED`, `ADMITTED`, `EXECUTING`, `VERIFYING`, `SUCCEEDED`, `FAILED`, `RECOVERY`).
- Implemented `BoundSkillToolBroker` scoped tool gateway.

#### B. Admission, Clarification & Telemetry Foundations
- Implemented `SkillAdmissionController` leveraging `SkillConcurrencyManager` and `ModelResidencyManager`.
- Implemented `ClarificationGate` with deterministic prompt generation.
- Implemented `ExperienceFeedbackService` recording decoupled `ExperienceEvent` records without automatic model self-modification.
- Implemented `SkillCheckpointMetadata` and `persist_skill_checkpoint` integration with `StateStore`.

## [Prompt 021B] — 2026-10-01

### Phase 5: SAGE Local Reasoning Model Provisioning

**Status:** IMPLEMENTED & VERIFIED — Qualified local quantized GGUF provisioned to SAGE-owned storage, llama-cpp-python runtime integrated, provider-neutral reasoning abstraction implemented, memory admission and residency policy integrated, offline deterministic smoke test verified, empirical qualification persisted.  
**Tests:** 362 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~36.7s) — 11 new Local Reasoning tests (`tests/test_local_reasoning.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (82 files)  
**Type Check:** `mypy` — 0 issues (80 source files)  

#### A. Model Provisioning & Integrity Verification
- Created SAGE-managed model directory outside OneDrive, repository, and C:\AEGIS:
  `%USERPROFILE%\.aegis\models\local-llm\qwen35\`
- Copied qualified artifact to SAGE-owned path: `qwen35-4b-q4km.gguf`
- Verified artifact properties:
  - Exact byte size: 2,740,937,888 bytes
  - SHA-256: `00FE7986FF5F6B463E62455821146049DB6F9313603938A70800D1FB69EF11A4`
  - GGUF architecture: `qwen35`, Q4_K_M quantization, 248,320 vocab tokens
- Original Meetly installation untouched and unmodified. No runtime dependency on Meetly or helper daemons.

#### B. Provider-Neutral Local Reasoning Abstraction (`src/reasoning/`)
- Created provider-neutral subsystem:
  - `src/reasoning/base.py`: `LocalReasoningProvider` abstract base class defining lifecycle (`initialize`, `is_ready`, `reason`, `unload`, `verify_integrity`, `get_metrics`).
  - `src/reasoning/models.py`: Typed Pydantic schemas (`ReasoningRequest`, `ReasoningResponse`, `ReasoningMetrics`, `ReasoningProviderStatus`).
  - `src/reasoning/errors.py`: Typed fail-closed error hierarchy (`ReasoningError`, `ReasoningStorageError`, `ReasoningIntegrityError`, `ReasoningAdmissionError`, `ReasoningExecutionError`, `ReasoningResidencyError`).
  - `src/reasoning/runtime.py`: Canonical runtime descriptor (`LLAMACPP_CPU_RUNTIME`), model descriptor (`QWEN35_4B_MODEL`), and skill profile (`SKILL_LOCAL_REASONING`).
  - `src/reasoning/provider.py`: `LlamaCppReasoningProvider` implementing CPU inference, streaming time-to-first-token (TTFT) measurement, deterministic temperature=0.0 generation, and storage boundary validation.
  - `src/reasoning/qualification.py`: Live empirical hardware qualification via `ModelHardwareQualifier`, saving profile to `%USERPROFILE%\.aegis\models\qualifications\qwen35-4b-q4km.qualification.json`.
  - `src/reasoning/__init__.py`: Public API exports and `build_default_reasoning_provider` factory.

#### C. Model Policy & Admission Integration
- Integrated with `SkillConcurrencyManager`: Dynamic RAM gating checks available host memory against 1.5 GB system reserve before admitting model weights. Fails closed with `ReasoningAdmissionError` if headroom is insufficient.
- Integrated with `ModelResidencyManager`: Tracks resident status, reference-counts active skill execution leases, and strictly blocks model eviction while active leases exist.
- Separated from Fast Decision Engine: Intended strictly for local Manager/reasoning inference; Fast Decision Engine remains advisory intent router.

#### D. Offline Smoke Test & Quality Gates
- Offline deterministic reasoning smoke test verified:
  - Cold load latency: ~1.8s
  - Time-To-First-Token (TTFT): ~600-630ms
  - Total inference latency: ~670-810ms (deterministic output verified)
  - Clean unload confirmed (zero memory leaks, residency cleared)
- Empirical hardware qualification profile persisted:
  - Measured resident RAM: ~2,740 MB
  - Measured marginal RAM: ~1,343 MB
  - Measured p95 latency: ~661 ms
  - Status: `QUALIFIED`

## [Prompt 021] — 2026-10-01

### Phase 5: Unified Supervisor + Execution State Recovery

**Status:** IMPLEMENTED & VERIFIED — Full Unified Supervisor and Execution State Recovery subsystem (`src/manager/`), SQLite migration `0006_task_execution_recovery_schema.sql`, crash discovery & idempotent recovery engine, and end-to-end integration tests.  
**Tests:** 351 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~16.0s) — 24 new Supervisor / Manager recovery tests (`tests/test_manager_recovery.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (74 files)  
**Type Check:** `mypy` — 0 issues (72 source files)  

#### A. Database Migration (`migrations/0006_task_execution_recovery_schema.sql`)
- Created 4 durable tables:
  - `supervisor_sessions`: Session lifecycle (`INITIALIZING`, `ACTIVE`, `RECONCILING`, `COMPLETED`, `FAILED`, `ABORTED`), metadata, error tracking.
  - `tasks`: Task state machine (`PENDING`, `PLANNING`, `EXECUTING`, `AWAITING_APPROVAL`, `COMPLETED`, `FAILED`, `CANCELLED`), priority, advisory route, error summary.
  - `task_steps`: Ordered execution steps (`PENDING`, `RUNNING`, `AWAITING_APPROVAL`, `COMPLETED`, `FAILED`, `SKIPPED`), action risk level, capability token ID, idempotency tracking, sanitized results.
  - `task_checkpoints`: Point-in-time recovery snapshots ordered monotonically (`checkpoint_seq`), with zero-secret snapshot verification.
- Enforced strict foreign keys, cascade rules, composite unique indexes (`UNIQUE(task_id, step_index)`, `UNIQUE(task_id, checkpoint_seq)`), and ISO 8601 UTC timestamps.

#### B. Manager Subsystem (`src/manager/`)
- **`src/manager/errors.py`**: Fail-closed exception hierarchy rooted in `ManagerError` (`StateTransitionError`, `TaskNotFoundError`, `StepNotFoundError`, `SessionNotFoundError`, `SecretLeakError`, `ReconciliationError`, `SupervisorExecutionError`).
- **`src/manager/models.py`**: Frozen Pydantic models and immutable enums. Implements valid state transition tables `VALID_TASK_TRANSITIONS` and `VALID_STEP_TRANSITIONS`, supporting human approval escalation workflows.
- **`src/manager/state_store.py`**: ACID SQLite state store utilizing `BEGIN IMMEDIATE` transactions to prevent concurrency conflicts. Enforces zero-secret policy via regex scanning on titles, metadata, arguments, snapshots, and error summaries. Sanitizes strings on read and write.
- **`src/manager/reconciliation.py`**: Startup crash recovery engine (`ReconciliationEngine`):
  - Discovers interrupted in-flight tasks (`EXECUTING`, `PLANNING`, `RUNNING` steps).
  - Idempotent steps with matching completed checkpoints are safely marked `COMPLETED`.
  - Incomplete idempotent steps are safely reset to `PENDING` for re-execution.
  - Ambiguous non-idempotent steps without checkpoints are escalated to `AWAITING_APPROVAL` with clear error summaries, strictly preventing duplicate side effects.
  - Corrupt or empty executing tasks safely transition to `FAILED` (fail closed).
  - Re-running reconciliation on stable database is provably idempotent.
- **`src/manager/supervisor.py`**: Central coordinator (`Supervisor`):
  - Plans tasks with ordered step definitions.
  - Queries `FastDecisionService` for advisory route suggestions (`is_advisory_only: Literal[True]` invariant strictly upheld).
  - Evaluates action risk with `TrustAuthority` (`evaluate_action_risk`).
  - Automatically executes approved steps via `ToolBroker` (`dispatch_tool`) or escalates unauthorized steps to `AWAITING_APPROVAL`.
  - Captures durable snapshots into `task_checkpoints` before and after dispatch.
  - Allows human operators to approve pending actions and resume execution.
- **`src/manager/__init__.py`**: Clean domain exports and `build_supervisor` factory helper.

#### C. Test Suite & Validation
- Added `tests/test_manager_recovery.py` with 24 comprehensive tests covering state machine transitions, store persistence, secret rejection, reconciliation scenarios, and end-to-end supervisor tool dispatch.
- Updated `tests/test_domain_schema.py` (55 tests) to expect schema version 6 and validate the 4 new tables.
- Updated `tests/test_environment_validation.py` (13 tests) recognizing `manager` as a canonical runtime package.

## [Prompt 020] — 2026-10-01

### Project Identity Migration (AEGIS → SAGE) + Tool Broker & Sandboxed Execution Foundation

**Status:** IMPLEMENTED & VERIFIED — Full SAGE identity migration across all active source files,
Tool Broker architecture with Trust Core integration, subprocess sandbox, output sanitization,
and comprehensive test suite.  
**Tests:** 327 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~15s) — 46 new Tool Broker tests (`test_tool_broker.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (67 files)  
**Type Check:** `mypy` — 0 issues (65 source files)  

#### A. SAGE Identity Migration

**Previous identity:** AEGIS — Autonomous Personal Digital Agent and Epistemic Knowledge Model  
**Current identity:** SAGE — System for Autonomous Guidance & Execution

- Updated `pyproject.toml` project metadata (name, description, authors).
- Updated `README.md` project overview and architecture summary.
- Added `SageConfig` / `SageBaseConfig` aliases in `src/config/models.py` and `src/config/__init__.py`.
- Added `SAGE_*` environment variable precedence in `src/config/loader.py`; `AEGIS_*` variables
  retained as legacy compatibility overrides (backward-compatible, documented).
- Added `get_default_sage_home()` in `src/storage/paths.py` checking `SAGE_HOME` then `AEGIS_HOME`.
- Created `config/sage.example.yaml` canonical configuration template.
- Updated module-level docstrings in `src/trust/`, `src/storage/`, `src/model_policy/`,
  `src/hardware/`, `src/benchmark/`, `src/config/`, `src/decision/`, `tests/`.
- Updated directory `README.md` headers throughout repository.
- Added `tests/test_sage_identity.py` — 5 identity compliance tests.
- Activation scripts (`activate_env.bat`, `activate_env.ps1`) updated to echo SAGE.

**Preserved as historical/compatibility (intentionally unchanged):**
- `C:\AEGIS` recovery path constant (`src/storage/paths.py:CANONICAL_RECOVERY_ROOT`) — must stay.
- `%USERPROFILE%\.aegis` runtime home — user data must not be invalidated.
- `AEGIS_*` environment variable keys in config loader — backward compatibility.
- `AEGIS_*.md` specification document filenames — historical frozen specs; not renamed.
- Migration SQL filenames (`0001_`–`0005_*.sql`) — Git history must not be rewritten.
- Test fixture data strings (`"AEGIS"`, `"Hello AEGIS"`) in `test_domain_schema.py` — valid test data.
- Physical repository directory `aeigs` — not renamed per explicit user instruction.

#### B. Tool Broker Implementation (`src/tools/`)

Five new modules implementing the full provider-independent Tool Broker:

- **`src/tools/errors.py`**: Fail-closed exception hierarchy (`ToolBrokerError` → registration,
  argument, execution, authorization, sanitization).
- **`src/tools/models.py`**: `ToolParameterSchema` (type coercion + bounds validation),
  `ToolDefinition` (static tool declaration with name regex enforcement), `ToolResult`
  (uniform return type), `ParamType` enum.
- **`src/tools/sanitizer.py`**: Best-effort output sanitizer — strips ANSI escapes, enforces 64 KiB
  / 2000-line limits, redacts AWS keys, bearer tokens, PEM private keys, GitHub tokens,
  and password/secret/token assignments. **Does NOT guarantee secret-free output; defense
  in depth only.**
- **`src/tools/subprocess_runner.py`**: Process-level subprocess wrapper — command allowlist
  enforcement from `ResourceScope`, `cwd` filesystem scope validation, credential-stripped
  environment (`_FORWARDED_ENV_KEYS` allowlist), `shell=False` enforced, timeout with
  `ToolTimeoutError`. **Explicitly NOT an OS-level sandbox** (no seccomp, AppArmor, containers,
  or Windows Job Objects). This is a process-level guard only.
- **`src/tools/broker.py`**: Central `ToolBroker` — full 6-step pipeline for every invocation:
  1. Tool registration lookup (fail-closed on unknown tool)
  2. Argument schema validation and coercion
  3. Trust Core capability request (via `TrustAuthority.request_capability`)
  4. Capability consumption (HMAC, expiry, revocation, scope, use-count checks)
  5. Sandboxed execution (inline handler or subprocess runner)
  6. Output sanitization before return
  Scope widening via override is rejected (fail-closed). All failures return `ToolResult`;
  exceptions never propagate to callers.
- **`src/tools/__init__.py`**: Public API surface.

#### C. Trust Core Integration

Tool Broker requires a `TrustAuthority` instance at construction time. Every `invoke()` call:
- Requests a capability for the tool's declared `action_name` and `ResourceScope`.
- Consumes the capability (single-use, 60 s TTL default) immediately before execution.
- Audit records are written for every ALLOW/DENY/ESCALATE decision by the Trust Core.
- Any `TrustError` (deny, approval required, capability lifecycle failure) → `ToolResult.denied`.

#### D. Security Review (verified before commit)

| Check | Status |
|---|---|
| Tool Broker cannot bypass TrustAuthority | ✅ Every invoke() path requests + consumes capability |
| Unknown tools fail closed | ✅ Returns ToolResult.error (ToolNotFoundError) |
| Invalid arguments fail closed | ✅ Returns ToolResult.error before auth check |
| Arbitrary shell execution impossible via broker | ✅ shell=False enforced; command allowlist required |
| shell=False enforced | ✅ subprocess_runner.py:134 |
| Executable allowlist enforced | ✅ ResourceScope.allowed_process_commands checked |
| cwd/resource scope enforced | ✅ realpath + scope.contains_path() |
| Environment credentials stripped | ✅ _FORWARDED_ENV_KEYS allowlist; no credential keys forwarded |
| Output bounded and sanitized | ✅ 64 KiB / 2000-line limits + secret pattern redaction |
| Tool output treated as untrusted | ✅ Always passes through sanitize_output() |
| Trust Core auth failure prevents execution | ✅ Capability consumed before execute(); any failure → DENIED |
| OS-level sandboxing NOT falsely claimed | ✅ Explicitly documented as process-level only |

#### E. Test Suite (`tests/test_tool_broker.py`)

46 new tests across 6 classes:
- `TestToolParameterSchema` (11): type coercion, bounds, allowed_values, bad type.
- `TestToolDefinition` (5): name validation, missing args, unknown keys, optional params, zero timeout.
- `TestToolResult` (4): success/error/denied/timeout constructors.
- `TestSanitizeOutput` (8): ANSI stripping, AWS/bearer/PEM/GitHub/password redaction, truncation, passthrough.
- `TestToolBrokerRegistration` (6): inline/subprocess registration, duplicate, no-handler, both-handlers errors.
- `TestToolBrokerInvoke` (9): success, unknown tool, missing arg, policy denial, scope widening rejection,
  scope narrowing allowed, output sanitization, handler exception, subprocess sandbox.
- `TestSubprocessRunner` (3): command not in allowlist, empty command, allowed command runs.

#### F. Files Created

- `src/tools/__init__.py`
- `src/tools/errors.py`
- `src/tools/models.py`
- `src/tools/sanitizer.py`
- `src/tools/subprocess_runner.py`
- `src/tools/broker.py`
- `tests/test_tool_broker.py`
- `config/sage.example.yaml`
- `tests/test_sage_identity.py`

## [Prompt 019] — 2026-10-01

### Deterministic Authority Layer & Trust Core Foundation

**Status:** IMPLEMENTED & VERIFIED — Full deterministic Trust Core foundation, HMAC-SHA256 capability tokens, fail-closed policy engine, append-only SQLite audit writer with zero-secret invariant, and schema migration 0005.  
**Tests:** 275 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, 14.80s) — 51 new comprehensive adversarial & functional tests (`test_trust_core.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (59 files)  
**Type Check:** `mypy` — 0 issues (57 source files)  
**Commit:** `4d47e72`

#### 1. Core Principle Enforcement
- *"Intelligence proposes; deterministic authority decides."*
- Full separation between LLM reasoning and authorization decisions. LLMs, model providers, skills, and agents are non-authorities and cannot grant themselves capabilities, modify policies, bypass checks, or access resources directly.
- Strict fail-closed semantics: all errors, missing parameters, forged tokens, or unrecognised actions default to DENY or ESCALATE (never silent allow).
- CRITICAL risk actions unconditionally require user approval (ESCALATE) across all autonomy tiers (Tier 0 to Tier 3).
- `policy.change` is permanently hard-denied (`PolicyDeniedError`) across all autonomy tiers without an approval path.

#### 2. Architecture & Modules Created (`src/trust/`)
- **`src/trust/errors.py`**: Granular exception hierarchy rooted in `TrustError`:
  - `CapabilityError` (subclasses: `CapabilityForgedError`, `CapabilityExpiredError`, `CapabilityRevokedError`, `CapabilityExhaustedError`, `CapabilityScopeError`).
  - `AuthorizationError` (subclasses: `PolicyDeniedError`, `ApprovalRequiredError`, `ApprovalExpiredError`, `ApprovalRejectedError`).
  - `AuditError`, `SecretLeakError`, `PolicyConfigurationError`.
- **`src/trust/capability.py`**:
  - `ActionRisk` enum (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`).
  - `ResourceScope` frozen dataclass for boundary enforcement (`filesystem_prefixes`, `allowed_network_hosts`, `allowed_process_commands`, `project_id`) with normalized path traversal protection (`posixpath.normpath`).
  - `Capability` frozen dataclass holding HMAC-SHA256 signature over canonical JSON payload.
  - `CapabilityFactory` generating process-local 32-byte cryptographically secure random keys (`os.urandom(32)`).
  - `CapabilityRegistry` tracking issued capabilities, in-process use counts, single-use/multi-use limits, and immediate revocation.
- **`src/trust/policy.py`**:
  - `AutonomyTier` enum (`0: STRICT_MANUAL`, `1: SAFE_BOUNDED`, `2: SUPERVISED`, `3: FULL_BOUNDED`).
  - `PolicyDecision` enum (`ALLOW`, `ESCALATE`, `DENY`).
  - `PolicyEngine` matching actions via longest prefix against canonical risk taxonomy (`_ACTION_RISK_TABLE`), defaulting unrecognised actions to `ActionRisk.CRITICAL`.
- **`src/trust/audit.py`**:
  - Append-only, tamper-evident audit log writer (`AuditWriter`) with immediate SQLite transaction writes.
  - Hard invariant: zero secret/credential material logged (`_check_no_secrets` scans key names, string values, and patterns for API keys, passwords, bearer tokens, and private keys, raising `SecretLeakError`). SHA-256 digests and hashes remain permitted.
- **`src/trust/authority.py`**:
  - `TrustAuthority` orchestrating policy evaluation, capability token issuance, consumption validation, and audit recording. Fail-closed: audit logging failures raise `AuditError` blocking capability issuance.
- **`src/trust/__init__.py`**:
  - Public export of all domain types and `build_trust_authority()` factory function.

#### 3. Database Migration (`migrations/0005_trust_core_schema.sql`)
- Created migration 0005:
  - `system_autonomy_policy`: Tracks global and project-scoped operational autonomy tiers.
  - `approval_requests`: Tracks human-in-the-loop approval workflows, approval states (`PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`), escalation tokens, and audit linkage.
  - `audit_records`: Append-only audit trail capturing event types (`AUTHZ_REQUEST`, `AUTHZ_DECISION`, `CAPABILITY_ISSUED`, `CAPABILITY_CONSUMED`, `CAPABILITY_DENIED`, `CAPABILITY_REVOKED`, `POLICY_CHANGE`), decision outcomes, actors, actions, scopes, and sanitised telemetry.

#### 4. Adversarial Test Suite (`tests/test_trust_core.py`)
- 51 exhaustive tests spanning 5 categories:
  - **TC-CAP (10 tests)**: Valid signatures, signature tampering rejection, cross-factory forgery rejection, TTL expiry, immediate revocation, use-count exhaustion, path/host/command scope boundaries, zero-scope boundary.
  - **TC-POL (13 tests)**: Autonomy tiers 0-3 vs risk levels LOW-CRITICAL, hard-deny `policy.change`, unrecognised action critical default, tier/risk decoupling.
  - **TC-AUDIT (7 tests)**: Record persistence, secret leakage rejection (`SecretLeakError`), API key/bearer token detection, SHA-256 digest acceptance, multiple record accumulation, redaction helper.
  - **TC-AUTH (9 tests)**: Orchestrated flow for allow/escalate/deny, consume audit records, revocation audit, approved escalation flow, factory builder.
  - **TC-ADV (12 tests)**: LLM self-authorization rejection, actor policy-change rejection, forged token consumption, replay attacks, path traversal attempts (`..`), network host wildcard injection, prompt injection in action strings, secrets in capability IDs, cross-project leakage, audit failure fail-closed behavior, expired/revoked precedence, policy change auditing.

#### 5. Architectural Status
- **DESIGNED:** Windows service broker, named pipe IPC, DPAPI/CNG hardware isolation adapter, Credential Broker, Tool Broker sandboxing.
- **IMPLEMENTED:** Deterministic Authority Layer, Capability tokens with HMAC-SHA256, Action-Risk Taxonomy, Autonomy Tier evaluation, Append-only Audit logging with zero-secret protection, SQLite schema migration 0005.
- **VERIFIED:** 51 Trust Core tests, 275 total system test suite passing 100% offline, ruff clean, mypy clean.
- **DEFERRED:** Platform-specific Windows service & kernel IPC enforcement (kept behind abstract interfaces).

---

## [Prompt 018] — 2026-10-01

### Controlled Model Provisioning & Empirical Hardware Qualification (Outcome B: Safe Defer)

**Status:** SAFE DEFER (Outcome B) — Storage preflight passed; model loading safely deferred due to non-negotiable host RAM reserve gate  
**Tests:** 224 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, 14.63s)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (52 files)  
**Type Check:** `mypy` — 0 issues (50 source files)  

#### 1. Live Hardware Measurements & Memory Gate
- **Total Physical RAM:** 15.69 GB (16,849,256,448 bytes)
- **Live Available RAM:** 2.49 GB (2,670,710,784 bytes)
- **Configured System RAM Reserve Floor:** 1.50 GB (1,610,612,736 bytes) — **Non-negotiable**
- **Live Usable RAM Headroom:** 0.99 GB (1,011.0 MB / 1,060,098,048 bytes)
- **Model Resident Load Requirement:** 3,200 MB resident + 64 MB marginal = 3,264 MB (~3.19 GB)
- **Headroom Shortfall:** 2,253 MB (~2.20 GB) below safe admission requirement
- **Total VRAM:** 6.00 GB (6,442,450,944 bytes) · **Available VRAM:** 5.53 GB (5,934,940,160 bytes)
- **Storage Preflight:** PASS (Host free: 80.76 GB > 40.00 GB floor; required: 7.42 GB permanent)

#### 2. Resource Gate & Safe Defer Determination
- In accordance with Section 2 of Prompt 018 and AEGIS resource-policy specifications, current available RAM does not satisfy the safe admission and provisioning threshold.
- The 1.50 GB system RAM reserve floor is strictly preserved and was not lowered or bypassed.
- Model initialization is not attempted, and zero synthetic/faked qualification profiles were created.
- Per Section 2 and Acceptable Outcomes, the system fails closed and executes **Outcome B (Safe Defer)**.

#### 3. Concurrency Qualification & Skill Admission
- **Lightweight Skills** (CPU-only, e.g. `SKILL_LIGHTWEIGHT_VALIDATOR`):
  - Admission status: **Permitted** (`permitted=True`)
  - Dynamic concurrency ceiling: **3 concurrent skills** (`min(3, 1 + (1,011 MB // 24 MB)) = 3`)
- **Heavyweight Model Skills** (requires model residency, e.g. `SKILL_VON_ROUTING`):
  - Admission status: **Denied / Blocked** (`permitted=False`)
  - Dynamic concurrency ceiling: **0 concurrent skills** (`max_safe_skills=0`)
  - Rationale: Live available RAM headroom (1,011 MB) < required memory (3,264 MB).
  - Degrades safely and non-blockingly to Manager fallback (`FallbackReason.ADMISSION_BUSY`).

#### 4. Verified Failure Behavior & Quality Gate Expansion
- Verified all 8 failure modes required by Section 8:
  1. *Missing artifact*: Fails closed with `verify_integrity() == False`, status `DISABLED`.
  2. *Corrupt/hash-mismatched artifact*: Detected and rejected by SHA-256 verification.
  3. *Insufficient RAM*: Dynamic memory gate blocks admission with `permitted=False`.
  4. *Insufficient VRAM*: Dynamic VRAM gate blocks admission with `permitted=False` (`test_21`).
  5. *Unavailable runtime*: Raises `ProviderUnavailableError` gracefully (`test_12`).
  6. *Initialization failure*: Smoke failure unloads backend and sets status `ERROR` (`test_13`).
  7. *Inference exception/error*: Returns `FallbackReason.INFERENCE_EXCEPTION`, releases inference lock and skill lease (`test_14`).
  8. *Concurrent admission exhaustion*: Non-blocking single-flight lock immediately falls back to `ADMISSION_BUSY`.
- Enhanced `VonFastDecisionProvider.unload()` to accept `target_status` to preserve explicit failure states (`ERROR`, `DISABLED`).

#### 5. Architectural Status
- **DESIGNED:** Local Fast Decision Engine architecture, Option-Marker joint attention, 2–3 skill concurrency target, 1.5 GB RAM reserve floor, 512 MB VRAM reserve floor, 40 GB host storage floor, 30 GB model storage budget.
- **IMPLEMENTED:** Model policy subsystem (`ModelStoragePolicy`, `LiveHardwareBenchmark`, `ModelHardwareQualifier`, `ModelResidencyManager`, `SkillConcurrencyManager`), Fast Decision provider (`VonFastDecisionProvider`), storage boundary enforcement, offline load verification, fail-closed error handling.
- **VERIFIED:** Reconciled manifest consistency, offline tokenizer operation, storage preflight checks, all 8 failure modes, 224 automated unit tests.
- **QUALIFIED:** Empirical live hardware detection and memory query, lightweight skill admission ceiling (3 concurrent skills).
- **DEFERRED:** Model weights download and live empirical resident load qualification deferred under **OUTCOME B (SAFE DEFER)** due to live RAM headroom constraint (0.99 GB available headroom vs 3.20 GB required).

---

## [Prompt 017] — 2026-10-01

### Model Manifest Reconciliation & Provisioning Gate (Outcome B: Safe Stop)

**Status:** SAFE STOP — Manifest fully reconciled; live model loading deferred (host RAM headroom insufficient)  
**Tests:** 220 passed · 0 failed · 0 skipped (100 % offline inside SAGE venv, 17.05s)  
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
**Tests:** 220 passed · 0 failed · 0 skipped (100 % offline inside SAGE venv)  
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