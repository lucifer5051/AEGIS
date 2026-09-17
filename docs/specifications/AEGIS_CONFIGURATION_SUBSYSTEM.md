# AEGIS CONFIGURATION SUBSYSTEM SPECIFICATION

**Document Version**: 1.0.0  
**Status**: ACTIVE / FOUNDATIONAL  
**Author**: AEGIS Architecture & Core Foundation Team  
**Scope**: Configuration Schemas, Precedence Model, Loading, Validation, and Invariants  
**Canonical Repository**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. Architectural Baseline and Philosophy

The AEGIS Configuration Subsystem provides a deterministic, typed, declarative configuration model for the AEGIS autonomous digital agent. It governs application settings, paths, resource allocations, operational policies, and hardware profiles.

### 1.1 Core Configuration Principles

1. **Configuration is NOT Security Authority**:
   - Configuration defines policy, limits, and preferred behaviors.
   - Configuration **never** grants authorization or circumvents security boundaries.
   - Trust Core remains the sole deterministic authority for capability gating, credential access, network enforcement, tool execution, and destructive action approval.
2. **Explicit and Typed**:
   - All settings belong to strictly typed Pydantic models with `extra="forbid"`.
   - Unknown or misspelled configuration keys are immediately detected and rejected.
   - Settings are never hidden inside opaque or untyped JSON blobs.
3. **Deterministic Precedence**:
   - Given identical configuration sources and precedence layers, the loader always computes an identical effective configuration object and configuration digest hash.
4. **Zero Secrets in Configuration Files**:
   - Configuration files (`.yaml`, `.json`) must never contain passwords, API tokens, encryption keys, private keys, or credentials.
   - Any attempt to provide sensitive keys in configuration triggers an immediate validation failure (`ConfigurationError`).
   - Secure secrets management is deferred to the Trust Core credential vault.
5. **No Dependence on AI Models**:
   - The configuration loader operates 100% offline without AI models, network access, or external service dependencies.

---

## 2. Configuration Precedence Model

The effective configuration is computed by layering configuration sources in strict order of increasing precedence. Higher-precedence layers override lower-precedence layers via recursive deep-merging.

```
+-------------------------------------------------------------+
| Layer 5: Explicit Runtime / User Overrides                  | (Highest Precedence)
+-------------------------------------------------------------+
                              ^
+-------------------------------------------------------------+
| Layer 4: Environment Variables (AEGIS_*)                    |
+-------------------------------------------------------------+
                              ^
+-------------------------------------------------------------+
| Layer 3: Environment-Specific YAML (aegis.<env>.yaml)       |
+-------------------------------------------------------------+
                              ^
+-------------------------------------------------------------+
| Layer 2: Static YAML Configuration (aegis.yaml)             |
+-------------------------------------------------------------+
                              ^
+-------------------------------------------------------------+
| Layer 1: Immutable Application Defaults                     | (Lowest Precedence)
+-------------------------------------------------------------+
```

### 2.1 Non-Overridable Invariants

Certain architectural invariants and constraints cannot be relaxed or overridden through ordinary user or environment configuration:
- Security policies requiring Trust Core approval for Tier 3 risk operations cannot be disabled by configuration.
- Operational tiers cannot exceed the system architectural boundaries.
- Offline constraints enforced by Trust Core cannot be bypassed by declaring `network.mode: ONLINE_ALLOWED` if physical or local policies forbid it.

---

## 3. Configuration Domains and Schema Architecture

The subsystem implements 13 structured configuration domains encapsulated within the top-level `AegisConfig` model:

### 3.1 Domain Overview

| Domain | Model Name | Description |
| :--- | :--- | :--- |
| **1. System** | `SystemConfig` | Schema versioning, environment (`development`, `staging`, `production`), diagnostics, and platform metadata. |
| **2. Paths** | `PathConfig` | Logical directory structure, relative/absolute path resolution, Windows path compatibility. |
| **3. Storage** | `StoragePolicyConfig` | Storage budgets, quotas, low-disk thresholds, and cleanup policies. |
| **4. Hardware Profile** | `HardwareProfile` | Static data structures representing CPU, RAM, GPU, Storage, OS, and Runtime. |
| **5. Hardware State** | `HardwareProfileState` | Architectural lifecycle states (`VALID`, `STALE`, `INVALID`, `REQUIRES_REBENCHMARK`). |
| **6. Provisioning** | `ProvisioningPolicyConfig` | Hardware and memory budgets for model and skill allocation. |
| **7. Autonomy** | `AutonomyConfig` | Operational tiers (`TIER_0` to `TIER_3`), bounded execution, and risk separation. |
| **8. Network** | `NetworkConfig` | Connectivity policies (`LOCAL_ONLY`, `PREFER_LOCAL`, `BALANCED`, `ONLINE_ALLOWED`). |
| **9. Channels** | `ChannelConfig` | Interaction channels (Desktop, WhatsApp) and channel affinity. |
| **10. Notifications** | `NotificationConfig` | Quiet hours, priorities, deduplication, batching, and approval alerts. |
| **11. Memory** | `MemoryConfig` | Context limits, token budgets, retrieval depth, and EKM retention policies. |
| **12. Logging** | `LoggingConfig` | Log levels, file retention, audit logging, and telemetry toggles. |
| **13. Developer** | `DeveloperConfig` | Safe debugging toggles (`debug_mode`, `test_mode`, `trace_execution`). |

---

## 4. Hardware Profile Architecture

The hardware profile subsystem defines data structures for machine characterization. It is purely a data representation layer; benchmark execution is handled by a separate subsystem.

### 4.1 Component Models

- `CpuProfile`: Vendor, model, physical cores, logical processors, base frequency (MHz), architecture.
- `RamProfile`: Total RAM (MB), available RAM (MB), memory pressure status.
- `GpuProfile`: Vendor, model, VRAM (MB), driver version, compute runtime support (CUDA, ROCm, etc.).
- `StorageDeviceProfile`: Mount point/drive letter, total capacity (GB), free capacity (GB), filesystem format.
- `OsProfile`: Operating system name, version, architecture, build number.
- `RuntimeProfile`: Python version, interpreter path, acceleration libraries.

### 4.2 Lifecycle States

Hardware profiles progress through four strictly defined states:
- `VALID`: Verified through benchmarking and matching current hardware.
- `STALE`: Profile exceeds maximum validity duration (default: 30 days) and requires verification.
- `INVALID`: Hardware configuration changes detected (e.g., changed CPU core count, swapped GPU, altered RAM).
- `REQUIRES_REBENCHMARK`: System migration, major driver update, or explicit benchmark invalidation.

---

## 5. Autonomy and Network Operational Tiers

### 5.1 Frozen Operational Autonomy Tiers

Autonomy represents the degree of automated execution permitted for tasks, kept strictly separate from risk classification:
- **Tier 0 — Strict Manual Approval**: Every action requires explicit user confirmation before execution.
- **Tier 1 — Safe / Bounded Execution**: Read-only actions and predefined safe local operations execute automatically; any state modification requires approval.
- **Tier 2 — Supervised Autonomy**: Standard workflows execute autonomously within bounded resources; policy-defined checkpoints or anomalies require user intervention.
- **Tier 3 — Full Bounded Task Autonomy**: Complex multi-step tasks execute end-to-end within strict pre-authorized scopes, resource budgets, and sandbox constraints.

### 5.2 Network Modes

Network modes declare desired connectivity preference:
- `LOCAL_ONLY`: Complete network isolation. Zero external network sockets or DNS lookups allowed.
- `PREFER_LOCAL`: External access permitted only when local equivalents are unavailable and authorized.
- `BALANCED`: Normal hybrid operation balancing local and external resources.
- `ONLINE_ALLOWED`: External network access permitted subject to standard Trust Core domain filtering.

*Note*: Declaring `ONLINE_ALLOWED` does not override Trust Core capability gating. Trust Core remains authoritative.

---

## 6. Path Resolution and Safety

Logical paths in `PathConfig` are defined relative to the canonical repository root or runtime working directory:
- Paths are validated against path traversal vulnerabilities.
- Explicit environment expansion is supported only when enclosed in `%VAR%` or `${VAR}` patterns for authorized path variables.
- The path resolver (`PathConfig.resolve_path()`) automatically resolves relative paths against the configured repository root, producing clean, normalized Windows paths.
- Machine-specific paths are never hard-coded into application schemas.

---

## 7. Environment Variable Overrides

The configuration loader inspects environment variables prefixed with `AEGIS_`. Supported overrides include:

| Environment Variable | Target Configuration Key | Type | Example |
| :--- | :--- | :--- | :--- |
| `AEGIS_ENVIRONMENT` | `system.environment` | String | `development` / `production` |
| `AEGIS_LOG_LEVEL` | `logging.log_level` | String | `DEBUG` / `INFO` / `WARNING` |
| `AEGIS_NETWORK_MODE` | `network.mode` | String | `LOCAL_ONLY` / `PREFER_LOCAL` |
| `AEGIS_AUTONOMY_TIER` | `autonomy.default_tier` | String | `TIER_0` / `TIER_1` |
| `AEGIS_DEBUG_MODE` | `developer.debug_mode` | Boolean | `true` / `false` |
| `AEGIS_TEST_MODE` | `developer.test_mode` | Boolean | `true` / `false` |
| `AEGIS_DATA_PATH` | `paths.data_path` | String | `data` |
| `AEGIS_RUNTIME_PATH` | `paths.runtime_path` | String | `runtime` |

---

## 8. Schema Canonicality and Artifacts

- **Canonical Source**: The Python Pydantic models in `src/config/models.py` are the single source of truth for all configuration schemas.
- **Generated Schema**: The JSON Schema located at `schemas/config.schema.json` is generated directly from `AegisConfig.model_json_schema()`, ensuring complete, synchronized type validation for IDE tooling, documentation, and external validators.
- **Example Blueprint**: `config/aegis.example.yaml` provides a fully commented, non-secret reference configuration conforming to the canonical schema.

---

## 9. Error Handling and Diagnostics

Configuration errors raise `ConfigurationError` instances with structured diagnostic metadata:
- Source location (file path or environment variable name).
- Target configuration field path (e.g., `storage.max_storage_gb`).
- Invalid value rejected.
- Expected type or constraint specification.
- Contextual error message with secrets stripped or redacted.
