"""SAGE Durable Workflow Engine -- DAG Validator.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 11
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 11

Enforces:
1. Structural integrity (non-empty steps, unique IDs, versioning).
2. Dependency completeness (no missing or dangling dependency references).
3. Directed Acyclic Graph (DAG) cycle detection (fails closed via CyclicDependencyError).
4. Reference coherence (input/output references must match declared dependencies).
5. Zero-Secret Invariant (strictly rejects embedded tokens, keys, passwords).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set

from src.trust.audit import _check_no_secrets
from src.workflow.errors import (
    CyclicDependencyError,
    WorkflowValidationError,
)
from src.workflow.models import WorkflowDefinition

_INTERPOLATION_PATTERN = re.compile(
    r"\{\{\s*steps\.([a-zA-Z0-9_\-]+)\.(?:output|outputs)\.([a-zA-Z0-9_\-]+)\s*\}\}"
)


def _find_interpolation_refs(data: Any) -> List[str]:
    """Recursively search for step dependency references in step arguments."""
    refs: List[str] = []
    if isinstance(data, str):
        for match in _INTERPOLATION_PATTERN.finditer(data):
            refs.append(match.group(1))
    elif isinstance(data, dict):
        for v in data.values():
            refs.extend(_find_interpolation_refs(v))
    elif isinstance(data, list):
        for item in data:
            refs.extend(_find_interpolation_refs(item))
    return refs


def _detect_cycles(steps_by_id: Dict[str, List[str]]) -> Optional[List[str]]:
    """Detect cycles in dependency graph using DFS with 3-color marking.

    Returns cycle path list if cycle exists, None otherwise.
    Graph representation: step_id -> list of dependencies (nodes it depends on).
    """
    # 0 = unvisited, 1 = visiting (in recursion stack), 2 = visited
    state: Dict[str, int] = dict.fromkeys(steps_by_id, 0)
    parent: Dict[str, Optional[str]] = dict.fromkeys(steps_by_id)

    for node in steps_by_id:
        if state[node] != 0:
            continue

        stack = [(node, iter(steps_by_id[node]))]
        state[node] = 1

        while stack:
            curr, children_iter = stack[-1]
            try:
                neighbor = next(children_iter)
                if neighbor not in state:
                    continue
                if state[neighbor] == 1:
                    # Cycle detected! Reconstruct cycle path
                    cycle = [neighbor, curr]
                    p = parent[curr]
                    while p is not None and p != neighbor:
                        cycle.append(p)
                        p = parent.get(p)
                    cycle.append(neighbor)
                    cycle.reverse()
                    return cycle
                if state[neighbor] == 0:
                    state[neighbor] = 1
                    parent[neighbor] = curr
                    stack.append((neighbor, iter(steps_by_id.get(neighbor, []))))
            except StopIteration:
                state[curr] = 2
                stack.pop()

    return None


def validate_workflow_definition(
    definition: WorkflowDefinition,
    skill_registry: Optional[Any] = None,
    tool_broker: Optional[Any] = None,
) -> None:
    """Validate a workflow definition against all structural, DAG, and security rules.

    Args:
        definition: The WorkflowDefinition to validate.
        skill_registry: Optional SkillRegistry for validating skill references.
        tool_broker: Optional ToolBroker for validating tool references.

    Raises:
        WorkflowValidationError: On missing steps, invalid references, or bad config.
        CyclicDependencyError: If a dependency cycle is found in the DAG.
        SecretLeakError: If any secret pattern is detected.
    """
    if not definition.workflow_id or not definition.workflow_id.strip():
        raise WorkflowValidationError("Workflow definition must have a non-empty workflow_id.")

    if definition.version < 1:
        raise WorkflowValidationError(f"Workflow version must be >= 1, got {definition.version}.")

    if not definition.name or not definition.name.strip():
        raise WorkflowValidationError("Workflow definition must have a non-empty name.")

    if not definition.steps:
        raise WorkflowValidationError(
            f"Workflow '{definition.workflow_id}' must define at least one step."
        )

    # 1. Zero-Secret Invariant Check across entire definition
    serialized_repr = json.dumps(
        {
            "workflow_id": definition.workflow_id,
            "name": definition.name,
            "description": definition.description,
            "metadata": definition.metadata,
            "steps": [s.model_dump() for s in definition.steps],
        },
        sort_keys=True,
    )
    _check_no_secrets(serialized_repr)

    step_ids: Set[str] = set()
    steps_dict: Dict[str, List[str]] = {}

    # 2. Step identity and contract validation
    for step in definition.steps:
        sid = step.step_id
        if not sid or not sid.strip():
            raise WorkflowValidationError("Step step_id must be a non-empty string.")

        if sid in step_ids:
            raise WorkflowValidationError(
                f"Duplicate step_id '{sid}' in workflow '{definition.workflow_id}'."
            )
        step_ids.add(sid)
        steps_dict[sid] = list(step.dependencies)

        if not step.skill_id and not step.tool_name and not step.action_name:
            raise WorkflowValidationError(
                f"Step '{sid}' must declare either skill_id, tool_name, or action_name."
            )

    # 3. Dependency existence and self-dependency check
    for step in definition.steps:
        for dep in step.dependencies:
            if dep == step.step_id:
                raise CyclicDependencyError(f"Step '{step.step_id}' cannot depend on itself.")
            if dep not in step_ids:
                raise WorkflowValidationError(
                    f"Step '{step.step_id}' references missing dependency '{dep}'."
                )

        # 4. Input/output reference checks
        referenced_steps = _find_interpolation_refs(step.arguments)
        for ref_sid in referenced_steps:
            if ref_sid not in step.dependencies:
                raise WorkflowValidationError(
                    f"Step '{step.step_id}' references output from '{ref_sid}', "
                    f"but '{ref_sid}' is not listed in its dependencies."
                )

    # 5. Cycle Detection (Topological DFS)
    cycle = _detect_cycles(steps_dict)
    if cycle:
        cycle_str = " -> ".join(cycle)
        raise CyclicDependencyError(
            f"Dependency cycle detected in workflow '{definition.workflow_id}': {cycle_str}"
        )

    # 6. Validate external contracts if registries provided
    for step in definition.steps:
        if skill_registry is not None and step.skill_id:
            try:
                skill_registry.get_skill(step.skill_id)
            except Exception as exc:
                raise WorkflowValidationError(
                    f"Step '{step.step_id}' references unregistered skill '{step.skill_id}': {exc}"
                ) from exc

        if tool_broker is not None and step.tool_name:
            if step.tool_name not in tool_broker.registered_tools():
                raise WorkflowValidationError(
                    f"Step '{step.step_id}' references unregistered tool '{step.tool_name}'."
                )
