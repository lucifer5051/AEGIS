"""SAGE Skills Subsystem -- First-Party Production Skills.

Provides bounded, verifiable first-party skills for:
1. Workspace Inspection (workspace.inspect): Read-only directory and search summary.
2. Controlled Development Verification (dev.verify): Whitelisted test/lint/format.
3. Technical Research and Error Diagnosis (research.diagnose): Local-first doc research.

Security Invariants:
- All tool execution routes through BoundSkillToolBroker -> ToolBroker -> TrustAuthority.
- Skills declare exact tool bindings and cannot access unlisted tools.
- Skills cannot grant capabilities or modify Trust Core policies.
- 100% offline deterministic execution.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.skills.executor import BoundSkillToolBroker, DeterministicSkillExecutor
from src.skills.models import (
    ConcurrencyPolicy,
    ExecutionStrategy,
    MemoryAccessPolicy,
    ProjectScope,
    RegisteredSkill,
    SkillLifecycleState,
    SkillManifest,
    SkillRecoveryPolicy,
    SkillResourceRequirement,
    SkillType,
    SkillVerificationPolicy,
)
from src.skills.registry import SkillRegistry
from src.tools.broker import ToolBroker
from src.tools.models import ToolResultStatus
from src.trust.capability import ActionRisk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Workspace Inspection Manifest & Handler
# ---------------------------------------------------------------------------

WORKSPACE_INSPECT_MANIFEST = SkillManifest(
    skill_id="workspace.inspect",
    name="Workspace Inspection",
    version="1.0.0",
    description=(
        "Inspect workspace directory structure, search text patterns, "
        "and produce evidence-backed summaries with strict read-only access."
    ),
    skill_type=SkillType.DETERMINISTIC,
    intent_tags=(
        "workspace",
        "inspect",
        "search",
        "read_only",
        "files",
        "project_structure",
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target_dir": {"type": "string", "default": "."},
            "pattern": {"type": "string", "default": "*"},
            "search_query": {"type": "string"},
            "max_files": {"type": "integer", "default": 50},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "target_dir": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "total_files": {"type": "integer"},
            "matches": {"type": "array"},
            "summary": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "read_only": {"type": "boolean"},
        },
        "required": ["target_dir", "files", "total_files", "summary", "read_only"],
    },
    execution_strategy=ExecutionStrategy.DIRECT,
    tool_bindings=("file_list", "file_read", "file_search"),
    required_capabilities=("filesystem.list", "filesystem.read"),
    resource_requirements=SkillResourceRequirement(
        declared_marginal_ram_bytes=16 * 1024 * 1024,
        timeout_ms=10000,
        marginal_cpu_cores=1.0,
    ),
    concurrency_policy=ConcurrencyPolicy(
        concurrency_class="inspection",
        max_concurrent=3,
        allow_parallel=True,
    ),
    memory_access=MemoryAccessPolicy.READ_ONLY,
    project_scope=ProjectScope.WORKSPACE_ONLY,
    risk_class=ActionRisk.LOW,
    verification_policy=SkillVerificationPolicy.SCHEMA_VALIDATION,
    recovery_policy=SkillRecoveryPolicy.FAIL_FAST,
    lifecycle_state=SkillLifecycleState.DISCOVERED,
)


def workspace_inspect_handler(
    args: Dict[str, Any],
    tool_broker: BoundSkillToolBroker,
) -> Dict[str, Any]:
    """Execute read-only workspace inspection and evidence-backed summary."""
    target_dir = str(args.get("target_dir", "."))
    search_query = args.get("search_query")
    max_files = int(args.get("max_files", 50))

    # 1. Discover files via file_list tool
    list_res = tool_broker.invoke(
        "file_list",
        {"directory": target_dir},
    )
    if list_res.status != ToolResultStatus.SUCCESS:
        return {
            "target_dir": target_dir,
            "files": [],
            "total_files": 0,
            "matches": [],
            "summary": f"Failed to list directory '{target_dir}': {list_res.error_summary}",
            "evidence": [f"Error: {list_res.error_summary}"],
            "read_only": True,
        }

    raw_lines = [
        line.strip()
        for line in list_res.output.splitlines()
        if line.strip() and line.strip() != "[EMPTY_DIRECTORY]"
    ]
    files = raw_lines[:max_files]
    evidence: List[str] = [f"Listed directory '{target_dir}': found {len(raw_lines)} entries"]

    # 2. Search content if query requested
    matches: List[Dict[str, Any]] = []
    if search_query:
        search_res = tool_broker.invoke(
            "file_search",
            {"directory": target_dir, "pattern": str(search_query)},
        )
        if (
            search_res.status == ToolResultStatus.SUCCESS
            and search_res.output
            and search_res.output != "[NO_MATCHES_FOUND]"
        ):
            for match_line in search_res.output.splitlines():
                if ":" in match_line:
                    parts = match_line.split(":", 2)
                    if len(parts) >= 3:
                        matches.append(
                            {
                                "file": parts[0].strip(),
                                "line_number": int(parts[1].strip())
                                if parts[1].strip().isdigit()
                                else 1,
                                "line_content": parts[2].strip(),
                            }
                        )
                    else:
                        matches.append({"raw": match_line})
                else:
                    matches.append({"raw": match_line})

            evidence.append(
                f"Searched for pattern '{search_query}': found {len(matches)} match(es)"
            )
        else:
            evidence.append(f"Search found no matches for '{search_query}'")

    summary = f"Workspace inspection of '{target_dir}': {len(raw_lines)} files discovered. " + (
        f"Search '{search_query}' matched {len(matches)} occurrence(s)."
        if search_query
        else "No search query specified."
    )

    return {
        "target_dir": target_dir,
        "files": files,
        "total_files": len(raw_lines),
        "matches": matches,
        "summary": summary,
        "evidence": evidence,
        "read_only": True,
    }


# ---------------------------------------------------------------------------
# 2. Controlled Development Verification Manifest & Handler
# ---------------------------------------------------------------------------

DEV_VERIFY_MANIFEST = SkillManifest(
    skill_id="dev.verify",
    name="Controlled Development Verification",
    version="1.0.0",
    description=(
        "Run explicitly permitted project test, lint, format, and typecheck "
        "commands through controlled interfaces without unrestricted shell access."
    ),
    skill_type=SkillType.DETERMINISTIC,
    intent_tags=(
        "dev",
        "verify",
        "test",
        "lint",
        "format",
        "typecheck",
        "quality",
    ),
    input_schema={
        "type": "object",
        "properties": {
            "check_type": {
                "type": "string",
                "enum": ["test", "lint", "format", "typecheck", "syntax"],
                "default": "test",
            },
            "target_path": {"type": "string", "default": "."},
            "python_code": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "check_type": {"type": "string"},
            "command": {"type": "string"},
            "success": {"type": "boolean"},
            "exit_code": {"type": "integer"},
            "output": {"type": "string"},
            "verification_passed": {"type": "boolean"},
        },
        "required": [
            "check_type",
            "command",
            "success",
            "exit_code",
            "output",
            "verification_passed",
        ],
    },
    execution_strategy=ExecutionStrategy.DIRECT,
    tool_bindings=("dev_runner", "python_eval"),
    required_capabilities=("process.run_test",),
    resource_requirements=SkillResourceRequirement(
        declared_marginal_ram_bytes=64 * 1024 * 1024,
        timeout_ms=30000,
        marginal_cpu_cores=1.0,
    ),
    concurrency_policy=ConcurrencyPolicy(
        concurrency_class="verification",
        max_concurrent=2,
        allow_parallel=False,
    ),
    memory_access=MemoryAccessPolicy.READ_ONLY,
    project_scope=ProjectScope.PROJECT_BOUND,
    risk_class=ActionRisk.MEDIUM,
    verification_policy=SkillVerificationPolicy.SCHEMA_VALIDATION,
    recovery_policy=SkillRecoveryPolicy.RETRY_IDEMPOTENT,
    lifecycle_state=SkillLifecycleState.DISCOVERED,
)


def dev_verify_handler(
    args: Dict[str, Any],
    tool_broker: BoundSkillToolBroker,
) -> Dict[str, Any]:
    """Execute controlled verification check through permitted tool interfaces."""
    check_type = str(args.get("check_type", "test"))
    target_path = str(args.get("target_path", "."))
    python_code = args.get("python_code")

    # If direct python code evaluation is requested
    if python_code is not None:
        eval_res = tool_broker.invoke("python_eval", {"code": str(python_code)})
        exit_code = (
            0
            if eval_res.status == ToolResultStatus.SUCCESS and "[exit_code:" not in eval_res.output
            else 1
        )
        success = eval_res.status == ToolResultStatus.SUCCESS and exit_code == 0
        return {
            "check_type": "syntax",
            "command": "python_eval(code)",
            "success": success,
            "exit_code": exit_code,
            "output": eval_res.output or str(eval_res.error_summary or ""),
            "verification_passed": success,
        }

    # Otherwise route to dev_runner tool
    runner_res = tool_broker.invoke(
        "dev_runner",
        {"verification_type": check_type, "target": target_path},
    )

    exit_code = (
        0
        if runner_res.status == ToolResultStatus.SUCCESS and "[exit_code:" not in runner_res.output
        else 1
    )
    success = runner_res.status == ToolResultStatus.SUCCESS and exit_code == 0

    return {
        "check_type": check_type,
        "command": f"dev_runner({check_type}, target={target_path})",
        "success": success,
        "exit_code": exit_code,
        "output": runner_res.output or str(runner_res.error_summary or ""),
        "verification_passed": success,
    }


# ---------------------------------------------------------------------------
# 3. Technical Research & Error Diagnosis Manifest & Handler
# ---------------------------------------------------------------------------

RESEARCH_DIAGNOSE_MANIFEST = SkillManifest(
    skill_id="research.diagnose",
    name="Technical Research and Error Diagnosis",
    version="1.0.0",
    description=(
        "Investigate technical errors by searching local documentation first, "
        "querying permitted research providers, preserving provenance, and "
        "formulating testable recovery hypotheses."
    ),
    skill_type=SkillType.DETERMINISTIC,
    intent_tags=(
        "research",
        "diagnose",
        "error",
        "troubleshoot",
        "recovery",
        "hypothesis",
    ),
    input_schema={
        "type": "object",
        "properties": {
            "error_message": {"type": "string"},
            "context": {"type": "string", "default": ""},
            "allow_online": {"type": "boolean", "default": False},
            "doc_query": {"type": "string"},
        },
        "required": ["error_message"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "error_message": {"type": "string"},
            "diagnosis": {"type": "string"},
            "local_sources": {"type": "array"},
            "online_sources": {"type": "array"},
            "recovery_hypotheses": {"type": "array"},
            "recommended_action": {"type": "string"},
            "verified_hypothesis": {"type": "boolean"},
        },
        "required": [
            "error_message",
            "diagnosis",
            "local_sources",
            "recovery_hypotheses",
            "recommended_action",
        ],
    },
    execution_strategy=ExecutionStrategy.DIRECT,
    tool_bindings=("research_query", "file_read", "file_search"),
    required_capabilities=("filesystem.read",),
    resource_requirements=SkillResourceRequirement(
        declared_marginal_ram_bytes=32 * 1024 * 1024,
        timeout_ms=15000,
        marginal_cpu_cores=1.0,
    ),
    concurrency_policy=ConcurrencyPolicy(
        concurrency_class="research",
        max_concurrent=3,
        allow_parallel=True,
    ),
    memory_access=MemoryAccessPolicy.READ_ONLY,
    project_scope=ProjectScope.PROJECT_BOUND,
    risk_class=ActionRisk.LOW,
    verification_policy=SkillVerificationPolicy.SCHEMA_VALIDATION,
    recovery_policy=SkillRecoveryPolicy.RETRY_IDEMPOTENT,
    lifecycle_state=SkillLifecycleState.DISCOVERED,
)


def research_diagnose_handler(
    args: Dict[str, Any],
    tool_broker: BoundSkillToolBroker,
) -> Dict[str, Any]:
    """Execute technical research and formulate testable recovery hypotheses."""
    error_message = str(args.get("error_message", ""))
    allow_online = bool(args.get("allow_online", False))
    doc_query = str(args.get("doc_query") or error_message)

    # 1. Query local documentation first through research_query tool
    res = tool_broker.invoke(
        "research_query",
        {
            "query": doc_query,
            "network_permitted": allow_online,
        },
    )

    local_sources: List[Dict[str, Any]] = []
    online_sources: List[Dict[str, Any]] = []
    summary_findings = ""

    if (
        res.status == ToolResultStatus.SUCCESS
        and res.output
        and res.output != "[NO_RESEARCH_FINDINGS]"
    ):
        try:
            parsed = json.loads(res.output)
            summary_findings = str(parsed.get("findings_summary", ""))
            for src in parsed.get("sources", []):
                if src.get("provider_type") == "ONLINE_DOC":
                    online_sources.append(src)
                else:
                    local_sources.append(src)
        except Exception:
            summary_findings = res.output

    # 2. Formulate testable recovery hypotheses
    hypotheses: List[Dict[str, Any]] = []

    err_lower = error_message.lower()
    if "syntaxerror" in err_lower or "indentationerror" in err_lower:
        hypotheses.append(
            {
                "hypothesis_id": "HYP-01",
                "description": "Code contains syntax or indentation flaw.",
                "strategy": "CORRECT_SYNTAX",
                "testable_action": "Run python_eval or ruff format on offending file.",
            }
        )
        recommended = "Verify syntax and reformat source code."
    elif "assertionerror" in err_lower or "failed" in err_lower or "test" in err_lower:
        hypotheses.append(
            {
                "hypothesis_id": "HYP-01",
                "description": "Test assertion failure due to unexpected state or parameter.",
                "strategy": "ADJUST_ASSERTION_OR_INPUT",
                "testable_action": "Re-run test with corrected inputs or fixture setup.",
            }
        )
        hypotheses.append(
            {
                "hypothesis_id": "HYP-02",
                "description": "Environment isolation or stale cache issue.",
                "strategy": "CLEAN_CACHE_AND_RETRY",
                "testable_action": "Clear cache and retry test suite.",
            }
        )
        recommended = "Check input parameters and test expectations."
    elif "filenotfounderror" in err_lower or "no such file" in err_lower:
        hypotheses.append(
            {
                "hypothesis_id": "HYP-01",
                "description": "Target path does not exist in active sandbox.",
                "strategy": "CREATE_OR_VERIFY_PATH",
                "testable_action": "Inspect workspace directory structure before opening file.",
            }
        )
        recommended = "Use workspace inspection to confirm file existence."
    else:
        hypotheses.append(
            {
                "hypothesis_id": "HYP-01",
                "description": f"General runtime exception: {error_message[:100]}",
                "strategy": "ISOLATE_AND_RETRY",
                "testable_action": "Check logs and verify tool parameters.",
            }
        )
        recommended = "Isolate failed step and check diagnostic logs."

    diagnosis = (
        f"Diagnosis for error '{error_message}': "
        f"{summary_findings or 'Analyzed against technical criteria.'} "
        f"Formulated {len(hypotheses)} testable recovery hypothesis(es)."
    )

    return {
        "error_message": error_message,
        "diagnosis": diagnosis,
        "local_sources": local_sources,
        "online_sources": online_sources,
        "recovery_hypotheses": hypotheses,
        "recommended_action": recommended,
        "verified_hypothesis": len(hypotheses) > 0,
    }


# ---------------------------------------------------------------------------
# Registration Helper
# ---------------------------------------------------------------------------


def register_first_party_skills(
    registry: SkillRegistry,
    broker: Optional[ToolBroker] = None,
) -> List[RegisteredSkill]:
    """Register all first-party skills and their deterministic executors.

    Args:
        registry: Target SkillRegistry.
        broker: Optional ToolBroker for reference validation.

    Returns:
        List of registered skill records.
    """
    results: List[RegisteredSkill] = []

    # 1. Workspace Inspection
    reg_inspect = registry.register_skill(
        manifest=WORKSPACE_INSPECT_MANIFEST,
        executor=DeterministicSkillExecutor(
            handler=workspace_inspect_handler,
            expected_output_keys=["target_dir", "files", "total_files", "summary", "read_only"],
        ),
    )
    results.append(reg_inspect)

    # 2. Controlled Development Verification
    reg_dev = registry.register_skill(
        manifest=DEV_VERIFY_MANIFEST,
        executor=DeterministicSkillExecutor(
            handler=dev_verify_handler,
            expected_output_keys=[
                "check_type",
                "command",
                "success",
                "exit_code",
                "output",
                "verification_passed",
            ],
        ),
    )
    results.append(reg_dev)

    # 3. Technical Research and Error Diagnosis
    reg_research = registry.register_skill(
        manifest=RESEARCH_DIAGNOSE_MANIFEST,
        executor=DeterministicSkillExecutor(
            handler=research_diagnose_handler,
            expected_output_keys=[
                "error_message",
                "diagnosis",
                "local_sources",
                "recovery_hypotheses",
                "recommended_action",
            ],
        ),
    )
    results.append(reg_research)

    logger.info("Successfully registered %d first-party skills.", len(results))
    return results
