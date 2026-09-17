# AEGIS HARDWARE DETECTION SUBSYSTEM SPECIFICATION

**Document Version**: 1.0.0  
**Status**: ACTIVE / FOUNDATIONAL  
**Author**: AEGIS Architecture & Core Foundation Team  
**Scope**: Read-Only Hardware Detection, Normalization, Diagnostics, and Profile Invariants  
**Canonical Repository**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. Purpose and Philosophy

The AEGIS Hardware Detection Subsystem provides deterministic, read-only, local inspection of host machine hardware and execution runtimes. It discovers system capabilities, normalizes component characteristics into canonical units, validates the resulting topology against the canonical `HardwareProfile` schema, and produces structured diagnostic records.

> [!IMPORTANT]
> **Fundamental Architectural Invariant**:
> **Hardware detection is NOT hardware benchmarking.**
> Hardware detection inspects static and point-in-time machine capabilities (core counts, RAM capacity, GPU models, driver versions, storage sizes). It does **not** execute computational stress tests, FLOPS benchmarks, memory bandwidth evaluations, or model throughput tests.

---

## 2. Scope

- Discovery of host Operating System, CPU topology, Physical/Available RAM, GPU accelerators, local storage volumes, and execution runtimes (Python, Node.js, Ollama, CUDA).
- Normalization of diverse raw formats (CSV, CIM JSON, Win32 structs) into standardized byte and megahertz quantities.
- Generation of deterministic, non-secret host node identifiers (`aegis-node-<hash>`).
- Structured component-level diagnostic reporting capturing outcome status, inspection provider, and execution durations.
- 100% offline, local-first execution with zero external network calls.

---

## 3. Non-Goals

The subsystem strictly avoids:
- **No Hardware Benchmarking**: No stress testing or compute performance profiling.
- **No Model Provisioning or Selection**: No model downloads, staging, or capability assignments.
- **No Security Authority**: The detector reports hardware facts; it does not grant permissions or authorize capabilities.
- **No Direct Persistence**: SQLite persistence and profile caching are deferred to subsequent storage subsystems.
- **No Continuous Monitoring**: This subsystem takes point-in-time snapshots, avoiding background telemetry loops.
- **No Hardware Monitoring Libraries**: Does not use heavy third-party packages (`psutil`, `GPUtil`, `pynvml`, `WMI`).

---

## 4. Provider Architecture

The subsystem employs a lightweight provider abstraction enabling platform-specific implementations while guaranteeing an identical interface and data model:

```
                  +--------------------------+
                  |     HardwareDetector     |  (Abstract Base Class)
                  +--------------------------+
                               ^
                               |
                  +--------------------------+
                  |  WindowsHardwareDetector |  (Windows 11 Native)
                  +--------------------------+
                               |
        +----------------------+----------------------+
        |                      |                      |
  [Python Stdlib]        [PowerShell CIM]       [nvidia-smi]
 (ctypes, platform)       (Win32 Classes)        (Optional)
```

### 4.1 Interface Contract
- `detect_os() -> (OSProfile, ComponentDiagnostic)`
- `detect_cpu() -> (CPUProfile, ComponentDiagnostic)`
- `detect_ram() -> (RAMProfile, ComponentDiagnostic)`
- `detect_gpus() -> (List[GPUProfile], ComponentDiagnostic)`
- `detect_storage() -> (List[StorageDriveProfile], ComponentDiagnostic)`
- `detect_runtimes() -> (RuntimeProfile, ComponentDiagnostic)`
- `detect_host_identity(os_prof, cpu_prof, ram_prof) -> str`
- `detect() -> HardwareDetectionResult`

---

## 5. Detection Sources and Mechanisms

| Component | Primary Source | Fallback Mechanism | Extracted Properties |
| :--- | :--- | :--- | :--- |
| **OS** | Python `sys.getwindowsversion()` | `platform.release()`, `platform.version()` | OS name, release version, build number, architecture (`64bit`). |
| **CPU** | PowerShell CIM `Win32_Processor` | Python `platform.processor()`, `os.cpu_count()` | Vendor, model name, physical cores, logical processors, base clock (MHz). |
| **RAM** | Windows API `GlobalMemoryStatusEx` | PowerShell CIM `Win32_OperatingSystem` | Total physical RAM (bytes), available RAM (bytes), memory load. |
| **GPU** | `nvidia-smi` CSV query (if present) | PowerShell CIM `Win32_VideoController` | Vendor, model, VRAM (bytes), driver version, CUDA compute capability. |
| **Storage** | PowerShell CIM `Win32_LogicalDisk` | Python `shutil.disk_usage()` | Drive ID (`C:`), filesystem (`NTFS`), total bytes, free bytes. |
| **Runtimes** | Python `platform.python_version()`, PATH checks | Tool `--version` queries (`node`, `ollama`, `nvcc`) | Python, Node.js, and Ollama versions, CUDA availability flag. |

---

## 6. Normalization Rules

To prevent unit ambiguity across different host environments, all values are normalized to explicit canonical units:
1. **Memory & Storage**:
   - VRAM from `nvidia-smi` (MiB) is multiplied by `1024 * 1024` into exact integer bytes.
   - CIM memory values (KiB) are multiplied by `1024` into exact integer bytes.
   - Disk space is reported strictly in integer bytes (`ge=1024*1024*1024`).
2. **Frequencies**:
   - Processor clock speeds are recorded in integer megahertz (MHz).
3. **Strings**:
   - Model strings are stripped of leading/trailing whitespace.
   - CPU vendors are normalized to standard identifiers (`Intel`, `AMD`, or raw manufacturer).

---

## 7. Profile State and Benchmark Separation

Newly detected profiles are assigned the canonical status:
```python
profile.status = HardwareProfileState.REQUIRES_REBENCHMARK
profile.invalidation_reason = "Initial hardware detection; requires baseline benchmark"
```
This guarantees that unbenchmarked hardware snapshots cannot masquerade as performance-certified hardware profiles.

---

## 8. Failure Handling and Fault Tolerance

The detector guarantees partial fault tolerance:
- If a secondary inspection tool (e.g. `nvidia-smi`) is missing or exits with a non-zero code, the detector falls back to CIM queries.
- If no discrete or integrated GPU is detectable, it returns a synthetic `GPUProfile(vendor="None", model="Unavailable", vram_bytes=0)` with status `UNAVAILABLE` rather than crashing or fabricating values.
- If PowerShell CIM is restricted or blocked, the detector utilizes standard library fallbacks (`ctypes`, `os.cpu_count()`, `shutil.disk_usage()`).
- The `is_complete` boolean in `HardwareDetectionResult` flags whether all core components succeeded without fallback degradation.

---

## 9. Security and Privacy Considerations

- **Read-Only**: The detector strictly inspects hardware metadata; it never modifies system configuration, driver files, or registry keys.
- **Safe Command Execution**: Commands are invoked via explicit argument lists without `shell=True`, preventing command injection. Strict timeouts (2.0s–5.0s) prevent hangs.
- **Non-Secret Host Identity**: Machine identifiers are derived via SHA-256 over non-sensitive structural properties (vendor, core count, RAM bucket, architecture). No Windows credentials, user account names, passwords, API tokens, or hardware serial numbers are collected or stored.

---

## 10. Test Strategy

1. **Deterministic Unit Tests**: 17 mock-driven tests verifying parsing of diverse JSON/CSV formats, fallback paths, timeout handling, malformed responses, partial detection, invalid numeric inputs, unit normalization, serialization, and secret exclusion.
2. **Live Windows Smoke Test**: Executes on the live host environment verifying end-to-end execution, schema compliance, and presence of core hardware entities without hard-coding specific hardware models.
3. **Zero Network Dependence**: All tests execute 100% offline without socket operations or web dependencies.
