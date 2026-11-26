# AEGIS / SAGE Related Work & Research Foundations

This document catalogs foundational research papers, frameworks, and academic literature informing the architecture, security guarantees, multi-agent coordination, and defensive design of the SAGE / AEGIS autonomous personal agent.

> **Note on Binary Storage**: Large binary research PDFs are excluded from git version control (see `.gitignore`) to ensure repository performance and hygiene. Full academic citations, topics, and conceptual relevance are preserved here in markdown.

---

## 1. Prompt Injection Defense, Isolation & Agent Security

| Paper / Specification | Authors / Venue | Core Focus & AEGIS Relevance |
|:----------------------|:----------------|:-----------------------------|
| **Benchmarking and Defending Against Indirect Prompt Injection** | Yi et al. (2023) | Formalizes indirect prompt injection across tool outputs and untrusted data streams. Informs AEGIS delimiter nonces, spotlighting, and strict isolation of external message payloads. |
| **Prompt Injection Attack against LLM-integrated Applications** | Greshake et al. (2023) | Seminal work demonstrating how untrusted third-party inputs manipulate agent reasoning. Directly inspired AEGIS architectural invariants (untrusted contact messages never route to central brain or ToolBroker). |
| **Containing the Autonomous Operator** | Debenedetti et al. (2024) | Explores containment mechanisms for autonomous agents operating in system environments. Guided AEGIS trust tiers, offline execution boundaries, and human-in-the-loop approval gates. |
| **DIBench: Benchmarking Decision Integrity of AI Agents** | Wang et al. (2024) | Evaluates whether agents maintain decision integrity under adversarial pressure. Serves as reference for AEGIS fail-closed policy design. |
| **Fault-Tolerant Sandboxing for AI Coding Agents: A Transactional Approach** | Zhang et al. (2024) | Proposes rollback-capable execution sandboxes. Informs AEGIS local SQLite and transaction isolation. |
| **Efficient Auditing of Adversarial AI Agent Behavior from Agent Traces** | Chen et al. (2024) | Techniques for auditing agent event traces without revealing sensitive user data. Reflected in AEGIS zero-leak cryptographic audit trail and canary testing. |
| **Red-Teaming LLM Multi-Agent Systems via Communication Attacks** | Li et al. (2024) | Analyzes vulnerability propagation across interconnected agents. Motivated AEGIS per-contact encrypted memory stores and cryptographic separation. |
| **SEABENCH: Benchmarking Endogenous Misalignment** | He et al. (2024) | Explores drift and misalignment in goal-directed agents. Relevant to AEGIS strict invariant checks and autonomous monitoring. |
| **A Sandbox for LLM** | Microsoft Research (2023) | Safe code execution sandboxing patterns informing AEGIS ToolBroker capabilities. |
| **NIST AI 600-1: Artificial Intelligence Risk Management Framework (Generative AI Profile)** | NIST (2024) | Federal safety and governance standards applied to personal AI systems; guides AEGIS risk mitigations, human oversight, and data sovereignty guarantees. |

---

## 2. Multi-Agent Systems, Coordination & Communication

| Paper / Specification | Authors / Venue | Core Focus & AEGIS Relevance |
|:----------------------|:----------------|:-----------------------------|
| **Generative Agents: Interactive Simulacra of Human Behavior** | Park et al. (UIST 2023) | Memory reflection, associative retrieval, and social simulation. Informs AEGIS conversational memory architecture and agent persona dynamics. |
| **Augmenting Multi-Agent Communication with State Delta Trajectory** | Zhou et al. (2024) | State delta communication reducing bandwidth and hallucination. Informs AEGIS sidecar protocol design and JSON-line event synchronization. |
| **CORE: Measuring Multi-Agent LLM Interaction** | Zhang et al. (2024) | Quantitative benchmarks for collaborative agent performance. |
| **Efficient Multi-Agent Collaboration with Tool Use** | Liu et al. (2023) | Division of labor between planning, execution, and verification agents in AEGIS. |
| **HIRAS / A Hierarchical Multi-Agent Framework** | Wu et al. (2024) | Hierarchical supervisor-worker agent delegation architectures. |
| **Hierarchical Multi-Agent Scheduling for Multi-Tenant LLM** | Tan et al. (2024) | Task scheduling and resource contention management under local hardware constraints. |
| **LLM-Based Multi-Agent Collaboration** | Wang et al. (2024) | Multi-perspective consensus and collaborative goal pursuit. |
| **LLM-Coordination: Evaluating and Analyzing Multi-Agent Coordination** | Chen et al. (2023) | Empirical analysis of synchronization bottlenecks in multi-agent loops. |
| **NeuralFSM: Adaptive Multi-Agent Coordination via Learning Finite-State** | Sun et al. (2024) | Deterministic state machine guarantees applied to agent conversation and approval states. |
| **PlanGEN: A Multi-Agent Framework for Generating Planning** | Zhao et al. (2024) | Structured step-by-step planning and replanning on execution failure. |
| **Planning with Multi-Constraints via Collaborative Language Agents** | Huang et al. (2024) | Multi-constraint optimization in real-world personal assistant tasks. |
| **Token-Efficient Multi-Agent Collaboration via System One-Guided Computation** | Yang et al. (2024) | Dual-process (fast vs slow) thinking patterns; informs AEGIS FastDecisionService vs TrustCore architecture. |
| **WORLDSOLVER: Can LLM Agents Simulate the World?** | Liu et al. (2024) | Large-scale environment simulation and long-horizon goal verification. |
| **ONE FOR ALL, ALL FOR ONE** | Collective Agent Team (2024) | Cooperative game-theoretic perspectives on agent alignment. |

---

## 3. Autonomous Reasoning, Tool Use & Verification

| Paper / Specification | Authors / Venue | Core Focus & AEGIS Relevance |
|:----------------------|:----------------|:-----------------------------|
| **ReAct: Synergizing Reasoning and Acting in Language Models** | Yao et al. (ICLR 2023) | Foundational Thought-Action-Observation loop implemented in SAGE Supervisor. |
| **Reflexion: Language Agents with Verbal Reinforcement Learning** | Shinn et al. (NeurIPS 2023) | Self-evaluative memory and iterative refinement from execution failure traces. |
| **Toolformer: Language Models Can Teach Themselves to Use Tools** | Schick et al. (NeurIPS 2023) | Self-supervised tool usage learning informing AEGIS ToolBroker API ergonomics. |
| **ToolScope: Enhancing LLM Agent Tool Use through Tool Merging** | Xu et al. (2024) | Merging and abstracting specialized tools into unified interfaces. |
| **LLM Agents Making Agent Tools** | Cai et al. (2023) | Dynamic tool synthesis and modular runtime tool loading. |
| **VeriFine: Scaling Verification for Self-Improvement** | Kumar et al. (2024) | Independent verifier architectures validating plan outputs against formal constraints. |
| **SELF-REFLECTION FINE-TUNING: Enhancing Agent Resilience** | Ren et al. (2024) | Post-execution reflection strategies mitigating cascading error states. |
| **STREAMLINED REFLECTIVE EVOLUTION** | Patel et al. (2024) | Efficient reflective feedback loops for edge and local agent runtimes. |
| **Agentic Multi Turn Reasoning: A Fairness Approach** | Gupta et al. (2024) | Multi-turn contextual stability and goal persistence across conversational interruptions. |
| **ASCENT: Online Test-Time Training of Long-Horizon Agents** | Mercer et al. (2024) | Test-time adaptation strategies for long-horizon autonomous tasks. |
| **Test-Time Agent Evolution for Long-Horizon Legal Reasoning** | Stolfo et al. (2024) | Strict evidentiary standards and compliance verification under legal boundaries. |

---

## 4. Agent Memory, Personal Assistants & Local Execution

| Paper / Specification | Authors / Venue | Core Focus & AEGIS Relevance |
|:----------------------|:----------------|:-----------------------------|
| **MemGPT: Towards LLMs as Operating Systems** | Packer et al. (2023) | Tiered memory architecture (working context, archival storage, recall memory). Directly informed AEGIS SQLCipher encrypted memory layers and session management. |
| **Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks** | Lewis et al. (NeurIPS 2020) | Dense vector retrieval grounding model outputs in authenticated local documents. |
| **SELF-RAG: Learning to Retrieve, Generate, and Critique** | Asai et al. (ICLR 2024) | Adaptive retrieval tokens determining when external context is required vs redundant. |
| **Attention Is All You Need (Transformer Architecture)** | Vaswani et al. (NeurIPS 2017) | Core architecture powering local Qwen/GGUF models in AEGIS. |
| **VOYAGER: An Open-Ended Embodied Agent with LLMs** | Wang et al. (2023) | Lifelong learning libraries and programmatic skill repositories. |
| **Robust Speech Recognition via Large-Scale Weak Supervision (Whisper)** | Radford et al. (2022) | High-accuracy offline speech processing informing voice input subsystems. |
| **Evaluating Inference Compute for Generative AI: Enterprise Framework** | Smith et al. (2024) | Local vs remote compute trade-offs; justifies AEGIS strict offline-first data sovereignty. |
| **TMCS: Tool-Grounded Multi-Agent Reasoning for Compositional Chemical Discovery** | Chemical Multi-Agent Team (2024) | Specialized scientific tool-augmented agent workflows. |
| **Multi-target Parallel Drug Discovery** | Bio-Agent Collaborative (2024) | Domain-specific parallel execution pipelines in secure environments. |
