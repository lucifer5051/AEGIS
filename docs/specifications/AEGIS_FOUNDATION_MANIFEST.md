# AEGIS Foundation Manifest

**Manifest Version**: 1.1.0  
**Generated Date**: 2026-09-17  
**Phase**: CANONICAL_REPOSITORY_RELOCATION  
**Prompt**: 003A  

---

## 1. Project Location & Repository Status
* **Canonical Project Root**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`
* **Previous Temporary Root**: `C:\AEGIS` (Preserved intact as temporary recovery copy)
* **Repository Relocation**: Completed
* **OneDrive Awareness & Architectural Decision**:
  * The canonical AEGIS development repository is intentionally located inside OneDrive at `C:\Users\digvi\OneDrive\Desktop\project\aeigs`.
  * From Prompt 003A onward, this path is the ONLY canonical development repository.
  * **Operational Risk Note**: OneDrive synchronization may interact with Git index lock files (`.git/index.lock`), SQLite WAL journal files, virtual environments, and high-frequency runtime cache/log files. These risks are acknowledged and will be handled through architecture/runtime decisions in subsequent phases.
* **Repository Status**: Valid local Git repository at `C:\Users\digvi\OneDrive\Desktop\project\aeigs\.git`. No remote configured. No commits created.
* **Ignore Configuration**: Standard Windows/Python `.gitignore` deployed at `C:\Users\digvi\OneDrive\Desktop\project\aeigs\.gitignore`.

---

## 2. Architecture Specification
* **Specification Document**: `docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md`
* **Specification Version**: `1.0.0-FROZEN`
* **Architectural Baseline**:
  * Dual-layer separation (Intelligence proposes, Deterministic Trust Core decides).
  * Manager-priority lifecycle (staged download, sandbox self-test, activation, health verification, then old Manager deletion).
  * Normal skill model update (delete old -> download new -> verify -> test -> activate -> fail-closed).
  * User-content backup policy (Authoritative user work backed up; AI weights, runtimes, and rebuildable indexes excluded).
  * Channel-affine routing (WhatsApp -> WhatsApp, Desktop -> Desktop) with explicit user-policy cross-channel notifications.
  * Operational autonomy frozen at Tiers 0 to 3, decoupling operational autonomy from action-risk classification.
  * Model implementation details strictly abstracted from user-facing controls.
* **Formal Amendments**:
  * **Amendment 001 (Performance Architecture)**:
    * *Identifier*: `AMENDMENT-001-PERFORMANCE`
    * *Date of Amendment*: 2026-09-20
    * *Document*: `docs/specifications/AEGIS_ARCHITECTURE_AMENDMENT_001_PERFORMANCE.md`
    * *Key Additions*:
      * **Fast Path Architecture**: Low-latency execution route prioritizing deterministic rules, cached state, and known workflows.
      * **Fast Decision Engine**: Advisory, lightweight routing abstraction for bounded classification without bypassing Trust Core.
      * **Performance Telemetry Architecture**: Pipeline stage latency measurement, zero-secret telemetry, and percentile tracking.

---

## 3. Host Platform Specifications (Prompt 001 Audit)
* **Operating System**: Microsoft Windows 11 Home Insider Preview Single Language (64-bit), Build `26220`
* **CPU**: 13th Gen Intel(R) Core(TM) i5-13450HX (10 Cores, 16 Logical Processors, ~2.40 GHz)
* **GPU**: NVIDIA GeForce RTX 3050 6GB Laptop GPU (6,144 MiB GDDR6 VRAM, Driver `616.92`, CUDA UMD `13.4`)
* **Physical Memory (RAM)**: 15.69 GB Visible (`16,454,352 KB`)
* **Primary Storage**: Drive C: (479.76 GB total, ~98.5 GB free space)
* **Host Runtimes Present**:
  * Python: 3.12.10 (Default on PATH)
  * Node.js: v24.13.0 / npm 11.6.2 / pnpm 10.31.0
  * .NET SDK: 10.0.300
  * Java: Eclipse Adoptium OpenJDK 25.0.2.10
  * Git: 2.52.0.windows.1 / GitHub CLI 2.88.0
  * FFmpeg: 8.1-full_build
  * Ollama CLI: 0.34.0 (Service inactive, no models downloaded)

---

## 4. Planned Language & Runtime Version
* **Target Python Version**: Python 3.12.x (Binding to `C:\Users\digvi\AppData\Local\Programs\Python\Python312\python.exe`)
* **Environment Strategy**: Isolated project-local virtual environment (`.venv`) to be established in a future prompt.

---

## 5. Known Machine Constraints & Mitigations
1. **6 GB VRAM Ceiling**:
   * *Constraint*: Cannot host multiple unquantized or 13B+ parameter models simultaneously.
   * *Mitigation*: Enforce quantized models (Q4_K_M / Q5_K_M); prioritize Manager residency; dynamic LRU eviction for skill models.
2. **High RAM Utilization**:
   * *Constraint*: ~1.92 GB free physical RAM available during baseline audit; memory compression active.
   * *Mitigation*: Lean process execution, memory-mapped storage, streaming context retrieval, avoid heavy in-memory data structures.
3. **No Native C/C++ or Rust Toolchains**:
   * *Constraint*: `cl.exe`, `gcc`, `clang`, `cargo`, and `rustc` are absent from PATH.
   * *Mitigation*: Restrict dependencies to pre-compiled binary wheels (e.g., PyTorch, NumPy, PyYAML, Pydantic).
4. **No Docker / WSL2**:
   * *Constraint*: Cannot execute Linux container stacks locally.
   * *Mitigation*: Design AEGIS as a native Windows application utilizing local process isolation.

---

## 6. Implementation Status
* **Models Installed**: None
* **Dependencies Installed**: None
* **Application Code**: None
* **SQLite Database**: None
* **Virtual Environment**: None
* **Recovery Copy**: `C:\AEGIS` preserved intact
* **Implementation Status**: NOT STARTED (Canonical repository relocation and foundation initialized only)

