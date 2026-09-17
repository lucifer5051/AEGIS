# AEGIS Development Environment Specification

**Document Version**: 1.0.0-ENV-SPEC  
**Status**: ACTIVE DEVELOPMENT ENVIRONMENT SPECIFICATION  
**Date**: 2026-09-17  
**Phase**: DEVELOPMENT_ENVIRONMENT_PROVISIONED  
**Canonical Project Root**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  

---

## 1. Python Runtime Specification
* **Target Version**: Python 3.12.x (Verified host installation: `Python 3.12.10`, MSC v.1943 64-bit AMD64).
* **Host Base Interpreter**: `C:\Users\digvi\AppData\Local\Programs\Python\Python312\python.exe`.
* **Package Installer**: `pip 25.0.1` (Isolated within venv).

---

## 2. Virtual Environment Location & OneDrive Architectural Strategy
* **Virtual Environment Path**: `%USERPROFILE%\.aegis\venv` (Resolved: `C:\Users\digvi\.aegis\venv`).
* **OneDrive Isolation Decision**:
  * The canonical repository source code, documentation, schemas, and tests reside in `C:\Users\digvi\OneDrive\Desktop\project\aeigs`.
  * The Python virtual environment directory contains thousands of rapidly changing `.py`, `.pyc`, `.exe`, and `.pyd` package files.
  * Placing the virtual environment at `C:\Users\digvi\.aegis\venv` isolates it **completely outside the Microsoft OneDrive synchronization root** (`C:\Users\digvi\OneDrive\`), permanently preventing:
    1. OneDrive file-locking conflicts (`EPERM` / `PermissionError`) during package installations and bytecode generation.
    2. Excessive background OneDrive network bandwidth consumption.
    3. Accidental source-control pollution or Git tracking of virtual environment internals.
* **Repository Coupling Invariant**: The repository does **not** hardcode machine-specific paths in source code or documentation. The environment is resolved deterministically via the user-agnostic `%USERPROFILE%\.aegis\venv` path or standard environment activation scripts.

---

## 3. Environment Activation & Usage
* **PowerShell**:
  ```powershell
  # From canonical repository root
  .\scripts\activate_env.ps1
  ```
* **Command Prompt (CMD)**:
  ```cmd
  scripts\activate_env.bat
  ```
* **Direct CLI Invocations**:
  ```powershell
  & "$env:USERPROFILE\.aegis\venv\Scripts\pytest.exe"
  & "$env:USERPROFILE\.aegis\venv\Scripts\ruff.exe" check .
  & "$env:USERPROFILE\.aegis\venv\Scripts\mypy.exe" tests
  ```

---

## 4. Selected Development & Validation Tooling

The development environment is intentionally minimal, restricted exclusively to core deterministic validation, static type checking, schema verification, and unit testing:

| Package | Installed Version | Purpose | Dependency Type | Python 3.12 Compatibility | Stability Expectation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`pytest`** | `9.1.1` | Unit test execution and assertion harness | Development / Test | Native prebuilt wheel | Long-term test infrastructure |
| **`pydantic`** | `2.13.5` | Data validation, schema definition, JSON Schema generation | Core Validation | Native prebuilt wheel (`pydantic_core 2.46.5`) | Core architectural schema foundation |
| **`jsonschema`**| `4.26.0` | Draft-7 & 2020-12 JSON Schema validation | Core Validation | Pure Python wheel | Stable validation tool |
| **`pyyaml`** | `6.0.3` | YAML configuration parsing | Core Validation | Prebuilt C-extension wheel | Standard configuration tool |
| **`ruff`** | `0.16.8` | Extremely fast linter and code formatter | Development / Lint | Single pre-compiled binary wheel | Primary code quality tooling |
| **`mypy`** | `2.3.1` | Static type checking and PEP-484 verification | Development / Types | Native prebuilt wheel | Primary static analysis tooling |

### Exact Transitive Dependencies (Frozen)
```text
annotated-types==0.8.0
ast_serialize==0.11.2
attrs==26.1.0
colorama==0.4.6
iniconfig==2.3.0
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
librt==0.15.0
mypy==2.3.1
mypy_extensions==1.1.0
packaging==26.3
pathspec==1.1.1
pip==25.0.1
pluggy==1.6.0
pydantic==2.13.5
pydantic_core==2.46.5
Pygments==2.21.0
pytest==9.1.1
PyYAML==6.0.3
referencing==0.37.0
rpds-py==2026.6.3
ruff==0.16.8
typing_extensions==4.16.0
typing-inspection==0.4.4
```

---

## 5. What is Intentionally NOT Installed Yet

To maintain architectural discipline and prevent bloated dependencies, the following frameworks are **explicitly excluded** from this prompt:
* **No AI / ML Frameworks**: PyTorch, TorchVision, TensorFlow, JAX, CUDA libraries, Transformers, HuggingFace Hub, GGUF runtimes, Ollama bindings.
* **No Vector Databases**: Chroma, Faiss, Qdrant, LanceDB.
* **No Application Frameworks**: FastAPI, Flask, Django, Uvicorn, Streamlit.
* **No Heavy Utility Libraries**: OpenCV, Pillow, Librosa, SoundFile, Playwright, Selenium.
* **No Native Compilers**: MSVC build tools, MinGW, Clang, Rust/Cargo.

---

## 6. Offline Execution & Security Invariant
* **No Network Dependency**: All installed tools, linters, type checkers, and test suites operate with **zero network connectivity**.
* **Zero Credential Exposure**: No `.env` files with secrets, API keys, or tokens exist in the repository.
* **Pure Isolated Tooling**: Python packages were sourced from verified prebuilt wheels; no system global site-packages are inherited (`include-system-site-packages = false`).

---
*End of Development Environment Specification.*
