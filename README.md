# AEGIS

AEGIS is a local-first personal autonomous agent designed to manage and assist with a user's digital environment through modular skills, controlled workflows, machine-first memory, adaptive configuration, and deterministic security boundaries.

---

## Overview

AEGIS is being developed as a unified, cohesive personal agent rather than a collection of disconnected assistants or ad-hoc scripts. It orchestrates digital life tasks, coordinates multi-step workflows, maintains long-term contextual memory, and interacts with local and external tools under strict governance.

The conceptual architecture flows hierarchically:

```
User
  ↓
Personal Agent (Unified Supervisor)
  ↓
Computer / Accounts / Workflows
  ↓
Intelligence + Tools
  ↓
Memory + Context
  ↓
Security / Permissions (Deterministic Boundary)
```

The fundamental architectural principle governing AEGIS is:

> **"Intelligence proposes; deterministic authority decides."**

Language models generate plans, interpret intentions, and draft actions. However, deterministic, non-probabilistic software systems validate parameters, verify capabilities, isolate credentials, evaluate risk boundaries, and execute actions.

---

## Current Project Status

> **Notice**: Architecture is substantially specified; implementation is ongoing in structured, verifiable milestone phases.

### Implemented

- **Foundation Architecture**: Comprehensive technical foundation specification, core principles, and repository manifest.
- **Repository Structure**: Canonical enterprise-grade directory structure with standardized sub-packages.
- **Database & Schema Architecture**: Machine-first schema architecture, 11-domain data classification matrix, SQLite WAL design, and cross-domain data relationship graphs.
- **Development Environment**: Isolated Python 3.12 environment, deterministic validation toolchain (`ruff`, `mypy`, `pytest`).
- **Declarative Configuration Subsystem**: Typed Pydantic configuration schemas across 13 domains, 5-tier deterministic precedence loader, Windows path safety, and zero-secret enforcement.
- **Hardware Profile Detection**: Read-only, deterministic inspection of CPU, RAM, GPU, storage, OS, and runtimes into validated `HardwareProfile` models with structured diagnostics.

### Not Yet Implemented

- Hardware benchmarking & performance calibration
- Model provisioning & resource budgeting runtime
- Trust Core runtime & capability authorization engine
- Manager / Supervisor execution loop
- Skills & Tool layer runtime
- SQLite database initialization & migrations
- External Knowledge Memory (EKM) retrieval runtime
- Workflow execution runtime & engine
- Durable scheduler & background job daemon
- Channel integrations (Desktop UI, WhatsApp transport)
- Computer-use automation & desktop interaction runtime
- Automated backup & restore engine

---

## Architecture

AEGIS is structured around modular, loosely coupled subsystems operating under centralized supervision:

- **Manager / Supervisor**: The central coordinator responsible for task triage, planning, step verification, resource allocation, and workflow sequencing.
- **Skills Architecture**: Modular execution units exposing typed actions, inputs, and outputs. Skills encapsulate specialized operational capabilities without bypassing security boundaries.
- **Trust Core Boundary**: The independent, deterministic authorization authority. All tool invocations, credential requests, filesystem modifications, and network egress actions must be explicitly authorized by Trust Core.
- **Machine-First EKM (External Knowledge Memory)**: A structured relational memory layer capturing entities, decisions, interactions, context, and operational histories.
- **Projects & Workflows**: First-class abstractions grouping related tasks, persistent context, artifacts, and execution graphs.
- **Hardware-Aware Provisioning**: Declarative resource budgeting ensuring model execution, memory consumption, and concurrency match host hardware capabilities.
- **Local-First Operation**: The core agent runs 100% locally on the host machine, requiring no external cloud services for baseline functionality.

---

## Security Model

Security in AEGIS is founded on the separation between probabilistic generation and deterministic authorization:

- **Deterministic Trust Core** (*Designed*): Non-bypassable security kernel enforcing authorization policies before any side-effecting action occurs.
- **Least Privilege** (*Designed*): Every skill and workflow operates under minimal required permissions.
- **Capability-Based Authorization** (*Designed*): Actions require explicit capability grants rather than broad ambient authority.
- **Fail-Closed Behavior** (*Designed*): Unrecognized actions, malformed inputs, or ambiguous policies default to immediate rejection.
- **Credential Isolation** (*Designed*): Secrets, tokens, and keys reside strictly within encrypted vaults managed by Trust Core; configuration and logs maintain a strict zero-secret invariant.
- **Network Policy Enforcement** (*Designed*): Declarative network modes (`LOCAL_ONLY`, `PREFER_LOCAL`, `BALANCED`, `ONLINE_ALLOWED`) enforced deterministically at runtime.
- **Auditability** (*Designed / Schema Specified*): Comprehensive, tamper-evident audit logging for all tool executions, approvals, and security events.

---

## Memory / Second Brain

The AEGIS memory architecture treats memory as a durable, structured, machine-first knowledge asset:

- **Structured Records**: Relational schema capturing discrete facts, task histories, workflow states, and user preferences.
- **Temporal Memory**: Time-indexed records with validity windows and supersession tracking.
- **Provenance & Lineage**: Every memory item records its origin, authoring process, and verification history.
- **Memory Trust Levels (M0 to M4)**: Formal trust graduation from unverified raw inputs (M0) to architecturally verified ground truth (M4).
- **Project-Scoped Context**: Context isolation preventing accidental cross-contamination between distinct project workspaces.
- **Derived Search & Vector Indexes**: Read-only derived search indexes and embeddings generated from canonical relational records.

---

## Hardware Awareness

AEGIS dynamically inspects and understands the host machine to make informed execution decisions:

- **Read-Only Inspection**: Hardware detection queries static system properties and point-in-time metrics without running invasive tests.
- **Hardware Profile**: Validated data structure representing CPU topology, physical/available RAM, discrete and integrated GPUs, storage volumes, OS build, and runtime environments.
- **Deterministic Normalization**: All capacities and frequencies are normalized into canonical integer bytes and megahertz.
- **Partial Fault Tolerance**: Missing or unsupported components (such as absent discrete GPUs) yield explicit `UNAVAILABLE` status records without interrupting system startup.
- **Benchmark Separation**:

> **"Hardware detection != hardware benchmarking."**

Detection discovers what hardware exists; benchmarking (Phase 3) measures how the hardware performs under computational load. Newly detected profiles carry the explicit status `REQUIRES_REBENCHMARK`.

---

## Development Environment

- **Primary Target**: Windows 11 (64-bit)
- **Runtime**: Python 3.12 (`%USERPROFILE%\.aegis\venv`)
- **Type Checking**: `mypy` (strict configuration)
- **Linting & Formatting**: `ruff` (line-length 100, strict checks)
- **Testing**: `pytest` (100% offline test execution)
- **Validation**: `pydantic` v2, `jsonschema`, `pyyaml`

---

## Repository Structure

```
aeigs/
├── docs/               # Technical specifications and architectural documents
│   ├── architecture/   # Cross-subsystem data relationship architectures
│   └── specifications/ # Foundational, schema, config, and hardware specs
├── src/                # Core AEGIS source code
│   ├── config/         # Declarative configuration subsystem & schemas
│   └── hardware/       # Read-only hardware profile detection subsystem
├── tests/              # Deterministic test suites (offline validation)
├── schemas/            # Canonical JSON schemas and manifest definitions
├── config/             # Example configuration templates (zero-secret)
├── scripts/            # Environment activation and management scripts
├── tools/              # Developer tooling and validation utilities
├── data/               # Persistent database and storage roots (git-ignored)
├── runtime/            # Ephemeral runtime scratchpad and cache (git-ignored)
├── migrations/         # Database migration scripts (staged for runtime)
├── benchmarks/         # Future hardware benchmarking definitions
├── diagnostics/        # Health check and diagnostic dump storage
└── deployment/         # Packaging and deployment artifacts
```

---

## Validation

All subsystems are backed by deterministic, offline test suites verified prior to every milestone:

- **48 tests passed** (0 failures, 0 skipped, 100% offline).
- `ruff check`: All checks passed (0 lint errors).
- `ruff format --check`: All files formatted to standard.
- `mypy`: 0 issues found across all source and test modules.
- **Zero Model Weights**: No AI models or binary checkpoints in repository.
- **Zero Databases**: No SQLite files created prematurely.
- **Zero Network Calls**: All test fixtures and detection routines execute fully offline.

---

## Roadmap

- **Phase 1 — Foundation Architecture**: Base specifications, development environment, schema design, configuration subsystem, hardware detection. *(Complete)*
- **Phase 2 — Core Runtime Infrastructure**: Storage manager, SQLite initialization, migration engine, base logging.
- **Phase 3 — Hardware Benchmarking & Provisioning**: Controlled micro-benchmarking, profile state machine transition, model capacity budgeting.
- **Phase 4 — Trust Core**: Cryptographic vault, deterministic authorization engine, capability gating, policy evaluator.
- **Phase 5 — Manager / Supervisor**: Planning loop, task execution state machine, failure recovery, plan evaluation.
- **Phase 6 — Skills & Tools**: Skill packaging format, execution sandbox, tool adapters, parameter validation.
- **Phase 7 — Memory / EKM Runtime**: Relational store implementation, provenance engine, temporal memory search.
- **Phase 8 — Workflows & Scheduling**: Multi-step DAG workflow engine, durable scheduler, recurring tasks.
- **Phase 9 — Channels & Computer Use**: Desktop shell integration, WhatsApp messaging bridge, guarded computer-use runtime.
- **Phase 10 — Backup / Recovery / Hardening**: Snapshot engine, disaster recovery, security auditing, production readiness.

---

## Development Philosophy

1. **Local-First**: Complete functionality without mandatory cloud or SaaS reliance.
2. **Machine-First Data Architecture**: Structured, schema-enforced relational state over unstructured logs.
3. **Deterministic Validation**: Rigorous typing, reproducible testing, and fail-closed validation.
4. **Modular Subsystems**: Explicit boundaries, minimal coupling, clean dependency graphs.
5. **Least Privilege**: Zero ambient authority; explicit, scoped permission grants.
6. **Explicit Permissions**: Actions requiring user approval cannot be silently bypassed.
7. **Graceful Failure**: Partial degradation and structured diagnostics over unhandled exceptions.
8. **No Unnecessary Dependencies**: Standard library and minimal, proven packages preferred.
9. **Measurable Implementation**: Objective verification gates before advancing project phases.

---

## License

License: Not yet selected.
