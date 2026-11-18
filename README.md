# SAGE — System for Autonomous Guidance & Execution

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![Tests Passing](https://img.shields.io/badge/tests-1148%20passed-brightgreen.svg)]()
[![Platform: Windows 11](https://img.shields.io/badge/platform-Windows%2011%20(64--bit)-lightgrey.svg)]()
[![License: Proprietary / Evaluative](https://img.shields.io/badge/license-Proprietary-red.svg)]()

> **"Intelligence proposes; deterministic authority decides."**

SAGE is a local-first, personal autonomous AI workstation and multi-agent coordination environment. It orchestrates digital workflows, manages long-term contextual memory, automates Windows desktop applications, interacts via text and voice, and executes complex tasks across local and cloud language models under unbypassable deterministic security governance.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Purpose & Design Philosophy](#2-purpose--design-philosophy)
3. [Architecture Overview](#3-architecture-overview)
4. [Central Brain & Independent Multi-Agent System](#4-central-brain--independent-multi-agent-system)
5. [Supervisor & ToolBroker Execution Model](#5-supervisor--toolbroker-execution-model)
6. [TrustCore Authorization & Security Boundaries](#6-trustcore-authorization--security-boundaries)
7. [ModelGateway & Multi-Model Dynamic Routing](#7-modelgateway--multi-model-dynamic-routing)
8. [Model Catalogue & Truthful Discovery](#8-model-catalogue--truthful-discovery)
9. [Local Model Downloads & Hugging Face Integration](#9-local-model-downloads--hugging-face-integration)
10. [Skills Marketplace & Model Assignment](#10-skills-marketplace--model-assignment)
11. [Unified Memory Engine](#11-unified-memory-engine)
12. [Desktop Awareness & UI Automation Subsystem](#12-desktop-awareness--ui-automation-subsystem)
13. [Friday Conversational Agent & Personas](#13-friday-conversational-agent--personas)
14. [Voice Interaction Subsystem](#14-voice-interaction-subsystem)
15. [PySide6 Desktop Application](#15-pyside6-desktop-application)
16. [Communication Integrations & Status](#16-communication-integrations--status)
17. [Installation & Prerequisites](#17-installation--prerequisites)
18. [Configuration & Credentials Setup](#18-configuration--credentials-setup)
19. [Running SAGE Locally](#19-running-sage-locally)
20. [Testing & Verification](#20-testing--verification)
21. [Current Limitations & Roadmap](#21-current-limitations--roadmap)
22. [Security & Privacy Considerations](#22-security--privacy-considerations)
23. [Contribution Guidelines](#23-contribution-guidelines)
24. [License Information](#24-license-information)

---

## 1. Project Overview

SAGE (**System for Autonomous Guidance & Execution**) is designed as an integrated desktop artificial intelligence workstation rather than a fragile chatbot wrapper or cloud-dependent SaaS tool. Operating natively on Windows 11, SAGE provides:

- A **PySide6 Native Desktop UI** with a 3-column conversation layout, live activity feed, execution step visualizer, model manager, skills marketplace, and 10-tab system settings.
- A **Hybrid Central Brain** that classifies user intentions, routes routine dialogue directly to conversation handlers, and delegates multi-step technical objectives to specialized autonomous agents.
- An unbypassable **TrustCore Authorization Engine** that guarantees probabilistic model outputs can never trigger destructive actions or egress sensitive data without explicit cryptographic/policy approval.
- An **Offline-First Architecture** featuring a resident zero-dependency local fallback engine (`sage-local-v1`), local GGUF model execution via llama.cpp/Ollama, and 100% offline-verifiable test suites (1,148+ tests).

---

## 2. Purpose & Design Philosophy

SAGE is built on six foundational engineering axioms:

1. **Intelligence Proposes; Deterministic Authority Decides**: Generative AI models draft plans, synthesize code, and predict user intent. Non-probabilistic, deterministic software evaluates permissions, verifies invariants, isolates credentials, and executes actions.
2. **Local-First & Offline Resilience**: Complete core functionality must operate without internet access or paid APIs. Resident offline models ensure immediate responses even during total network severance.
3. **Machine-First Structured State**: Epistemic knowledge, operational records, and task states are persisted in structured, relational SQLite tables with DPAPI cryptographic protection, rather than ephemeral unstructured text.
4. **Agent Independence with Controlled Coordination**: Autonomous agents maintain isolated memory namespaces, private sessions, and designated model configurations. Cross-agent coordination is strictly mediated through acyclic, bounded delegation graphs.
5. **Truthful Subsystem Health Reporting**: SAGE never hallucinates subsystem availability, displays fake health badges, or markets incompatible models as ready. If an Ollama daemon is offline or a cloud key is unconfigured, it is truthfully marked `UNAVAILABLE` or `NOT_CONFIGURED`.
6. **Zero-Secret Invariant**: API keys, credentials, and tokens are stored in encrypted vaults and sanitized prior to log emission or UI presentation.

---

## 3. Architecture Overview

SAGE is organized into clearly delimited architectural tiers:

```
┌────────────────────────────────────────────────────────────────────────┐
│                      PySide6 Windows Desktop UI                        │
│  [TopBar] [Sidebar] [ConversationView] [ModelsView] [SkillsView] [...] │
└────────────────────────────────────┬───────────────────────────────────┘
                                     │ User Messages / Actions
                                     ▼
┌────────────────────────────────────────────────────────────────────────┐
│                          Hybrid Central Brain                          │
│   - Fast Decision Engine (Lightweight Intent Classifier)               │
│   - ModelGateway Routing (@model overrides, candidate pools)           │
│   - DelegationEngine (Acyclic multi-agent delegation)                  │
└────────────┬───────────────────────┬──────────────────────────┬────────┘
             │ Conversational        │ Complex Execution        │ Direct Tool
             ▼                       ▼                          ▼
┌─────────────────────────┐ ┌───────────────────┐ ┌──────────────────────┐
│  Friday / Agent Persona │ │    Supervisor     │ │  Desktop Awareness   │
│  - Session History      │ │  - Task Synthesizer│ │  - C# WinUI Watcher  │
│  - Contextual Memory    │ │  - Checkpoint DAG │ │  - UI Automation API │
└─────────────────────────┘ └─────────┬─────────┘ └──────────┬───────────┘
                                      │                      │
                                      ▼                      ▼
┌────────────────────────────────────────────────────────────────────────┐
│                          ToolBroker Gateway                            │
│           (Filesystem, Shell, Desktop, Process, Diagnostics)           │
└────────────────────────────────────┬───────────────────────────────────┘
                                     │ Verification & Capability Tokens
                                     ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   TrustCore Security Boundary (Authoritative)          │
│   - Deterministic Capability Evaluation                                │
│   - Interactive ApprovalStore (User confirmation prompt)               │
│   - DPAPI Key Rotation & Hardware Keystore                             │
└────────────────────────────────────┬───────────────────────────────────┘
                                     │
                                     ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   Unified Memory & Persistence Engine                  │
│   - SQLite WAL with SQLCipher / DPAPI encryption                       │
│   - Hybrid Retrieval (FTS5 BM25 + Vector Embeddings + Graph Links)     │
│   - Isolated Namespaces (agent:<id>, contact:<jid>, user:default)      │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Central Brain & Independent Multi-Agent System

The Central Brain (`src/brain/central_brain.py`) serves as SAGE's global orchestration layer:

- **Intent Classification**: Evaluates incoming requests across categories (`CONVERSATIONAL`, `STATUS_QUERY`, `TASK_CANCELLATION`, `ACTIONABLE_TASK`, `MULTI_AGENT_DELEGATION`).
- **Clean Dialogue Separation**: Pure conversational queries (`hi`, `how are you`, `explain quantum computing`) bypass the execution queue entirely and route directly to conversational handlers without creating unwanted pending tasks.
- **Independent Agent Roster**:
  - **Friday (`friday`)**: Default conversational and technical assistant.
  - **Atlas (`atlas`)**: Deep researcher and literature analyst.
  - **Operator (`operator`)**: Desktop application controller and UI automation specialist.
  - **Inspector (`inspector`)**: Code reviewer, test verification, and syntax validator.
- **Acyclic Delegation (`src/brain/delegation.py`)**: Central Brain delegates sub-goals to independent agents while enforcing recursion depth limits (max 5) and cycle prevention.
- **WhatsApp Contact Routing (`src/brain/whatsapp_router.py`)**: Routes incoming contact messages to lightweight fast models while reserving high-parameter reasoning models for escalated operational requests.

---

## 5. Supervisor & ToolBroker Execution Model

Executable technical objectives are managed by the Supervisor (`src/manager/supervisor.py`):

- **Task Synthesizer**: Decomposes user goals into directed acyclic dependency graphs (DAG) of discrete steps.
- **Checkpoint Hierarchy (`src/manager/state_store.py`)**: Persists task progress, retry attempts, and rollback snapshots at every milestone step.
- **ToolBroker (`src/tools/broker.py`)**: Provides sandboxed execution adapters for CLI tools, shell scripts, filesystem operations, and desktop automation.
- **Failure Recovery & Replanning**: Automatically detects tool execution failures, analyzes error outputs, adjusts dependency parameters, and generates alternate execution paths.

---

## 6. TrustCore Authorization & Security Boundaries

TrustCore (`src/trust/`) is authoritative, non-bypassable, and deterministic:

- **Strict Separation of Concerns**: Probabilistic models cannot authorize their own actions. Every side-effecting operation (filesystem write, external process launch, desktop mouse click) requires a cryptographically validated capability token.
- **Approval Store**: High-risk operations (file deletion, network modification, execution of unsigned scripts) trigger interactive user confirmation prompts in the UI desktop pane.
- **DPAPI Key Rotation**: Windows Data Protection API (DPAPI) encrypts the master database key, rotated deterministically via `src/security/database_key_rotation.py`.
- **Zero Ambient Authority**: Agents execute with minimal necessary permissions.

---

## 7. ModelGateway & Multi-Model Dynamic Routing

The ModelGateway (`src/models/gateway.py`, `src/brain/central_brain.py`) enables dynamic runtime switching across heterogeneous providers:

- **Multi-Candidate Candidate Pools**:
  - `balanced`: Primary reasoning tier (Default: `qwen2.5-7b-instruct` or `qwen35-4b-q4km`).
  - `reasoning`: Deep chain-of-thought analysis (e.g. `deepseek-r1-distill-qwen-7b`).
  - `fast`: Low-latency conversational dialogue (e.g. `phi-4-mini`).
  - `coding`: Specialized programming and refactoring (e.g. `deepseek-coder-6.7b`).
  - `local`: Guaranteed resident offline execution (`sage-local-v1`).
  - `cloud`: Cloud provider gateway fallback (`gemini`, `openai`, `anthropic`, `groq`).
- **Per-Message Directive Overrides**: Users can direct individual messages to specific models using inline prompts (e.g. `@model Qwen3 explain...`, `@model deepseek refactor...`, `@model Auto ...`).
- **Fallback Chains**: Automatic failover from primary GGUF models to resident offline engines upon timeouts or resource exhaustion.

---

## 8. Model Catalogue & Truthful Discovery

SAGE maintains an authoritative 12-category model catalogue (`src/models/catalog.py`) with at least 3 verified candidates per category:

| Category | Primary Candidate | Secondary Candidates |
|---|---|---|
| **General Brain** | `qwen2.5-7b-instruct` | `gemma-2-9b-it`, `phi-4-mini` |
| **Reasoning** | `deepseek-r1-distill-qwen-7b` | `deepseek-r1-distill-llama-8b`, `qwen2.5-14b-instruct` |
| **Coding** | `deepseek-coder-6.7b` | `qwen2.5-coder-7b`, `codestral-22b` |
| **Vision** | `llava-v1.6-mistral-7b` | `minicpm-v-2.6`, `bakllava-1` |
| **Audio** | `whisper-small-en` | `whisper-base-en`, `moonshine-base` |
| **Multimodal** | `llava-1.6-mistral-7b` | `LFM3-Omni-2B`, `minicpm-v-2.6` |
| **Embeddings** | `bge-small-en-v1.5` | `all-minilm-l6-v2`, `nomic-embed-text-v1.5` |
| **Reranking** | `bge-reranker-v2-m3` | `cross-encoder-ms-marco-minilm-l6`, `jina-reranker-v2` |
| **Translation** | `opus-mt-en-hi` | `nllb-200-distilled-600m`, `madlad400-3b-mt` |
| **Documents** | `docling-ibm` | `got-ocr2`, `layoutlmv3-base` |
| **Tool Calling** | `mistral-7b-instruct-v0.3` | `hermes-3-llama-3.1-8b`, `qwen2.5-7b-instruct` |
| **Specialized** | `whisper-small-en` | `LFM3-Omni-2B-research`, `paraphrase-minilm` |

### Architectural Evaluation: LiquidAI/LFM3-Omni-2B
Liquid AI's 2.0B non-transformer liquid neural network is explicitly catalogued:
- **Capabilities**: Interleaved text, image, and audio input processing.
- **Runtime Constraints**: Non-transformer dynamical state-space layers are incompatible with standard upstream llama.cpp GGUF operators. Requires PyTorch or LiquidSDK. Truthfully catalogued as `DISCOVERED` under research status rather than a falsified ready GGUF.

---

## 9. Local Model Downloads & Hugging Face Integration

The Hugging Face Manager (`src/models/huggingface.py`, `src/skills/marketplace.py`):

- **Hardware Compatibility Engine**: Automatically evaluates model memory footprints against host resources (defaulting to NVIDIA RTX 3050 4GB/6GB VRAM + 16GB RAM).
- **Offload Tiers**:
  - `PERFECT_GPU_FIT`: 100% GPU offload inside VRAM with KV-cache headroom.
  - `PARTIAL_GPU_OFFLOAD`: Split VRAM layers with system RAM spillover.
  - `CPU_RAM_ONLY`: Safe execution within host system RAM.
  - `INSUFFICIENT_MEMORY`: Blocked from download with explicit warning.
- **Cancellable Background Downloads**: Manages segmented GGUF weight retrieval with pause, resume, and progress tracking.

---

## 10. Skills Marketplace & Model Assignment

SAGE provides an extensible skills architecture (`src/skills/marketplace.py`):

- **Built-in First-Party Skills**:
  - `workspace.inspect`: Codebase indexing, AST parsing, symbol search, and file tree exploration.
  - `dev.verify`: Syntax verification, automated test suite execution, and regression auditing.
  - `research.diagnose`: Log parsing, error stack tracing, and causal diagnostic analysis.
  - `desktop.automation`: Windows UI Automation, window focus, keyboard typing, and mouse control.
- **Per-Skill Model Candidate Pools**: Each skill defines $\ge 3$ compatible candidate models and persists user-assigned models (`assigned_model`) inside the SQLite `skills_registry` table.
- **Dynamic Skill Assignment**: The desktop UI allows users to bind specific skills to preferred models or maintain adaptive `Auto` routing.

---

## 11. Unified Memory Engine

SAGE's second-brain memory runtime (`src/memory/engine.py`):

- **Hybrid Retrieval**: Blends SQLite FTS5 BM25 keyword matching with dense vector embedding similarity.
- **Relational Knowledge Graph**: Models entities, relationships, attributes, and temporal validity intervals.
- **Context IR Builder**: Dynamically packages conversation context, active project files, and retrieved memories within token budgets.
- **Memory Trust Tiers**: Distinguishes raw inputs ($M_0$), synthesized summaries ($M_1$), confirmed facts ($M_2$), and system invariants ($M_4$).
- **Strict Namespace Isolation**: Partitions memory scopes across agents (`agent:<id>`), external contacts (`contact:<jid>`), and host user (`user:default`).

---

## 12. Desktop Awareness & UI Automation Subsystem

The Desktop Awareness subsystem (`src/desktop/`, `tools/sage-desktop-watcher/`) allows SAGE to inspect and control the Windows desktop:

- **Native C# UI Automation Sidecar (`sage-desktop-watcher`)**: High-performance Windows Accessibility API watcher built on .NET 9.0 that tracks window focus changes, element bounding boxes, and accessibility hierarchy trees with zero polling overhead.
- **Differential Tracking (`differential_tracker.py`)**: Computes semantic UI diffs when application screens refresh.
- **Desktop Action Adapter (`action_adapter.py`)**: Performs precise mouse clicks, text input, window maximization, and shortcut dispatching under TrustCore capability checks.
- **Interruption Guard**: Automatically pauses automation if the user physically moves the mouse or strikes a key.

---

## 13. Friday Conversational Agent & Personas

- **Friday**: SAGE's primary conversational agent. Speaks with a professional, succinct technical tone. Gathers user intent, presents actionable alternatives, and transparently coordinates background execution.
- **Multi-Agent Personas**: Users can seamlessly switch conversations to Atlas (Research), Operator (Automation), or Inspector (Verification), each maintaining private message histories and distinct visual badges.
- **Optimistic UI Rendering**: User messages appear immediately upon hitting Enter; agent responses stream into Markdown message cards displaying the exact model used (e.g. `[sage-local-v1]`, `[qwen35-4b-q4km]`).

---

## 14. Voice Interaction Subsystem

SAGE includes native voice I/O capabilities (`src/voice/service.py`):

- **Physical Hardware Detection**: Discovers local microphones (e.g. `boAt Rockerz 510`, `Intel Smart Sound Array`) and audio outputs via `PySide6.QtMultimedia.QMediaDevices`.
- **Push-to-Talk (PTT)**: Captures 16kHz 16-bit mono PCM audio on demand via `QAudioSource`.
- **Transcription Adapter**: Converts captured audio into text for prompt execution.
- **Windows SAPI Speech Synthesis**: Non-blocking text-to-speech output with configurable rate (-10 to 10), volume (0 to 100), interrupt/stop capabilities, and UI testing controls.

---

## 15. PySide6 Desktop Application

The standalone native desktop interface (`src/ui/desktop/`, `sage_desktop.py`):

- **Sidebar Navigation**: Instant switching across 10 dedicated views:
  - `Conversation`: 3-column chat layout with sessions search, model selector, PTT mic button, and collapsible Execution Inspector.
  - `Agents`: Agent roster, personas, and live delegation states.
  - `Workflows`: Active execution DAGs and dependency visualization.
  - `Tasks`: Historical task records with step-by-step audit logs.
  - `Models`: Truthful model inventory, runtime filters, hardware assessment card, and routing alias settings.
  - `Skills`: Installed skills, candidate pools, and Hugging Face model downloads.
  - `Memory`: Knowledge graph search, facts explorer, and memory namespaces.
  - `Artifacts`: Generated documents, code patches, and exportable outputs.
  - `Status`: Live subsystem health matrix and hardware resource monitors.
  - `Settings`: 10-tab workstation configuration (General, Agents, Models, Skills, Voice, Appearance, Integrations, Memory & Privacy, Security & TrustCore, Advanced).
- **Execution Inspector**: Live real-time visualization of Supervisor plan steps, running tools, and capability tokens.

---

## 16. Communication Integrations & Status

SAGE includes communication gateway abstractions (`src/communication/`, `src/gateway/`):

| Integration | Implementation Status | Description |
|---|---|---|
| **PySide6 Native Desktop UI** | **Implemented & Live Tested** | Primary desktop application with real-time bidirectional signals. |
| **Resident Fallback Engine** | **Implemented & Live Tested** | Zero-dependency local model adapter (`sage-local-v1`). |
| **Local GGUF (llama.cpp)** | **Implemented & Verified** | High-performance quantized local inference via llama.cpp. |
| **Ollama Daemon Adapter** | **Implemented & Verified** | Local daemon connection with offline discovery failover. |
| **Cloud Gateways (Gemini/OpenAI/Anthropic)** | **Implemented** | Cloud provider adapters; requires user-provided API keys. |
| **WhatsApp Transport** | **Architecture & Protocol Implemented** | Node.js Baileys sidecar bridge with isolated contact memory namespaces. |
| **Telegram Adapter** | **Designed / Stubbed** | Extensible bot adapter interface ready for deployment. |

---

## 17. Installation & Prerequisites

### Prerequisites
- **Operating System**: Windows 11 (64-bit) (or Windows 10 build 19041+)
- **Python**: Version 3.12 (Isolated virtual environment recommended: `%USERPROFILE%\.aegis\venv`)
- **.NET Runtime**: .NET 9.0 Windows Desktop Runtime (for UI Automation sidecar)
- **C++ Build Tools**: Visual Studio 2022 C++ Build Tools (for local llama-cpp-python compilation if desired)
- **Hardware Recommended**: 16 GB System RAM, NVIDIA RTX 3050 (4GB/6GB VRAM) or higher.

### Environment Setup

```powershell
# Clone the repository
git clone https://github.com/lucifer5051/AEGIS.git C:\Users\digvi\OneDrive\Desktop\project\aeigs
cd C:\Users\digvi\OneDrive\Desktop\project\aeigs

# Create and activate virtual environment
python -m venv C:\Users\digvi\.aegis\venv
C:\Users\digvi\.aegis\venv\Scripts\Activate.ps1

# Install project dependencies
pip install --upgrade pip
pip install -e .
```

---

## 18. Configuration & Credentials Setup

SAGE uses a 5-tier deterministic configuration system (`src/config/`):

1. **Environment Variables**: Prefixed with `SAGE_` (e.g. `SAGE_NETWORK_MODE=LOCAL_ONLY`).
2. **Local Configuration File**: `config/sage.yaml` (overrides default templates).
3. **Hardware Profile**: Automatically detected on startup.
4. **Zero-Secret Defaults**: All default configuration templates contain zero hardcoded secrets.

To configure optional cloud provider credentials:

```powershell
# Optional: Set API keys via user environment variables (never committed to git)
[System.Environment]::SetEnvironmentVariable("GEMINI_API_KEY", "your-gemini-key", "User")
[System.Environment]::SetEnvironmentVariable("OPENAI_API_KEY", "your-openai-key", "User")
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "your-anthropic-key", "User")
```

---

## 19. Running SAGE Locally

### Quick Launch (Windows Desktop)
Double-click the desktop launcher:
```powershell
.\Launch_SAGE.bat
```
Or launch directly from PowerShell:
```powershell
C:\Users\digvi\.aegis\venv\Scripts\python.exe sage_desktop.py
```

### Headless Verification Mode
To verify desktop components offscreen:
```powershell
$env:QT_QPA_PLATFORM="offscreen"
C:\Users\digvi\.aegis\venv\Scripts\python.exe sage_desktop.py --test-mode
```

---

## 20. Testing & Verification

SAGE includes a comprehensive 100% offline test suite. No internet connection or paid API keys are required:

```powershell
# Activate virtual environment
C:\Users\digvi\.aegis\venv\Scripts\Activate.ps1

# Run the complete test suite (1,151 tests)
$env:QT_QPA_PLATFORM="offscreen"
pytest -q

# Run targeted subsystem test suites
pytest tests/test_prompt081_intelligence_and_voice.py -v
pytest tests/test_central_brain_and_multi_agent.py -v
pytest tests/test_desktop_app.py -v
pytest tests/test_trust_core.py -v
pytest tests/test_model_gateway.py -v
```

**Verification Status**: **1,148 passed, 3 skipped, 0 failures** across 1,151 collected tests.

---

## 21. Current Limitations & Roadmap

- **Simulated HF Downloads in Test Mode**: Model weight downloads are simulated in testing environments to prevent multi-gigabyte disk consumption during automated regression runs.
- **LiquidAI/LFM3-Omni-2B GGUF Support**: Requires official upstream llama.cpp support for liquid neural operators or installation of the custom PyTorch LiquidSDK adapter.
- **WhatsApp Live Daemon**: Node.js Baileys sidecar protocol is implemented; live connection requires active phone QR code scanning.
- **Continuous Speech Streaming**: Current voice implementation uses Push-to-Talk (PTT); continuous wake-word listening is planned for future milestones.

---

## 22. Security & Privacy Considerations

- **Strict Local Boundaries**: SAGE defaults to `LOCAL_ONLY` network policy, preventing any external egress unless explicitly reconfigured by the user.
- **Cryptographic Key Storage**: Master encryption keys are protected using the Windows Data Protection API (DPAPI).
- **Audit Trails**: All executed commands, tool inputs, and user approvals are recorded in an append-only audit table.
- **Credential Sanitization**: The UI and logs automatically mask string patterns matching API keys, passwords, and tokens.

---

## 23. Contribution Guidelines

1. **Adhere to the Core Invariant**: Never allow probabilistic model outputs to bypass TrustCore capability checks.
2. **Maintain Offline Test Integrity**: All new tests must execute 100% offline without external network dependencies.
3. **Preserve Repository Hygiene**: Never commit `.env` files, API keys, SQLite databases, or large model weight binaries (`.gguf`, `.safetensors`).
4. **Follow Typing & Formatting Standards**: Code must pass `ruff` formatting and `mypy` strict type checking.

---

## 24. License Information

Copyright © 2026 SAGE Project Contributors.  
This project is distributed under a proprietary / evaluative development license. All rights reserved. Commercial redistribution or cloud hosting without authorization is prohibited.
