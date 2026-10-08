"""SAGE Provider-Neutral Structured Planner.

Translates ContextIR into a typed, validated machine-checkable Plan
using the active LocalReasoningProvider.

Core Invariants:
1. Intelligence proposes; deterministic authority decides.
2. Structured output validation against typed Pydantic models (no fragile regex).
3. Tools verified strictly against ToolBroker registry.
4. Bounded repair: exactly one repair attempt on validation failure, then fail-closed.
5. No empty or impossible plans.
6. Zero secret leakage.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field

from src.manager.context import ContextIR
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import ReasoningRequest
from src.storage.paths import (
    is_path_inside_onedrive,
    is_path_inside_recovery_repo,
    is_path_inside_repo,
)
from src.tools.broker import ToolBroker
from src.tools.errors import ToolArgumentError, ToolNotFoundError
from src.trust.capability import ActionRisk

logger = logging.getLogger(__name__)


class PlannedStep(BaseModel):
    """Single discrete machine-checkable execution step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_number: int = Field(ge=1, description="1-indexed execution sequence number.")
    description: str = Field(min_length=1, description="Human-readable step intent.")
    tool: str = Field(min_length=1, description="Exact name of registered tool in ToolBroker.")
    arguments: Dict[str, Any] = Field(
        default_factory=dict, description="Dictionary of named arguments matching tool schema."
    )
    is_idempotent: bool = Field(
        default=False,
        description="Whether executing this step multiple times yields identical state.",
    )
    acceptance_criteria: str = Field(
        min_length=1,
        description="Deterministic condition used by the verifier to assess step success.",
    )
    estimated_risk: ActionRisk = Field(
        default=ActionRisk.MEDIUM, description="Estimated risk classification tier."
    )
    target_resource: str = Field(
        default="*", description="Target filesystem path, host, or command string."
    )


class Plan(BaseModel):
    """Complete machine-checkable execution plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: str = Field(min_length=1, description="Original user goal statement.")
    rationale: str = Field(min_length=1, description="Strategic overview of the planned steps.")
    steps: List[PlannedStep] = Field(
        min_length=1, description="Ordered non-empty sequence of discrete execution steps."
    )


class PlanValidationError(RuntimeError):
    """Raised when a candidate plan fails structural, semantic, or boundary validation."""


class StructuredPlanner:
    """Provider-neutral planner generating strictly validated task plans."""

    def __init__(self, provider: LocalReasoningProvider) -> None:
        self.provider = provider

    def generate_plan(self, context_ir: ContextIR, broker: ToolBroker) -> Plan:
        """Generate a validated Plan from ContextIR using local reasoning.

        On the first validation failure, attempts one repair query with error
        feedback. If the repair also fails, fails closed with PlanValidationError.

        Args:
            context_ir: Bounded context assembly.
            broker: ToolBroker providing the authoritative tool catalog.

        Returns:
            Validated Plan instance.

        Raises:
            PlanValidationError: If valid plan cannot be produced or verified.
        """
        system_prompt = self._build_system_prompt()
        prompt = self._build_planning_prompt(context_ir, broker)

        # First planning attempt
        req = ReasoningRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=2048,
            response_format={"type": "json_object"},
        )
        response = self.provider.reason(req)

        try:
            raw_obj = self._parse_json(response.content)
            plan = Plan.model_validate(raw_obj)
            self.validate_plan(plan, broker)
            return plan
        except Exception as first_error:
            logger.warning(
                "Initial plan candidate failed validation: %s. Attempting bounded repair.",
                first_error,
            )

            # Repair attempt (bounded to 1)
            repair_prompt = self._build_repair_prompt(
                goal=context_ir.active_goal,
                failed_output=response.content,
                error_message=str(first_error),
                broker=broker,
            )
            repair_req = ReasoningRequest(
                prompt=repair_prompt,
                system_prompt=system_prompt,
                temperature=0.0,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )
            repair_response = self.provider.reason(repair_req)

            try:
                repaired_obj = self._parse_json(repair_response.content)
                repaired_plan = Plan.model_validate(repaired_obj)
                self.validate_plan(repaired_plan, broker)
                logger.info("Plan repair succeeded.")
                return repaired_plan
            except Exception as repair_error:
                logger.error(
                    "Plan repair failed closed: %s. (Initial failure: %s)",
                    repair_error,
                    first_error,
                )
                raise PlanValidationError(
                    f"Failed to generate valid plan after repair attempt. "
                    f"Initial error: {first_error}; Repair error: {repair_error}"
                ) from repair_error

    def validate_plan(self, plan: Plan, broker: ToolBroker) -> None:
        """Deterministically validate plan steps, arguments, and boundaries.

        Args:
            plan: The Plan model to validate.
            broker: The ToolBroker against which tools and arguments are checked.

        Raises:
            PlanValidationError: On any unknown tool, malformed step, invalid args,
                                 or illegal capability scope.
        """
        if not plan.steps:
            raise PlanValidationError("Plan must contain at least one step.")

        for idx, step in enumerate(plan.steps, start=1):
            if step.step_number != idx:
                raise PlanValidationError(
                    f"Step sequence error: step at position {idx} has "
                    f"step_number {step.step_number}. "
                    "Steps must be sequentially numbered starting from 1."
                )

            # 1. Reject unknown tools
            try:
                tool_def = broker.get_definition(step.tool)
            except ToolNotFoundError as exc:
                available = sorted(broker.list_tools().keys())
                raise PlanValidationError(
                    f"Unknown tool '{step.tool}' at step {idx}. Available tools: {available}"
                ) from exc

            # 2. Reject malformed / missing required arguments
            try:
                tool_def.validate_args(step.arguments)
            except ToolArgumentError as exc:
                raise PlanValidationError(
                    f"Invalid arguments for tool '{step.tool}' at step {idx}: {exc}"
                ) from exc

            # 3. Reject illegal capability scopes (e.g. forbidden paths)
            self._validate_step_scope(step)

    def _validate_step_scope(self, step: PlannedStep) -> None:
        """Verify step arguments and target resource do not violate security boundaries."""
        # Check target_resource and any string/path arguments
        candidates: List[str] = []
        if step.target_resource and step.target_resource != "*":
            candidates.append(step.target_resource)

        for val in step.arguments.values():
            if isinstance(val, str) and ("\\" in val or "/" in val):
                candidates.append(val)

        for path_str in candidates:
            try:
                p = Path(path_str).resolve()
                if is_path_inside_onedrive(p):
                    raise PlanValidationError(
                        f"Illegal capability scope: path '{path_str}' resides inside OneDrive."
                    )
                if is_path_inside_repo(p):
                    raise PlanValidationError(
                        f"Illegal capability scope: path '{path_str}' "
                        "resides inside the repository."
                    )
                if is_path_inside_recovery_repo(p):
                    raise PlanValidationError(
                        f"Illegal capability scope: path '{path_str}' "
                        "resides inside recovery repository."
                    )
                # Check C:\AEGIS forbidden
                if str(p).upper().startswith("C:\\AEGIS"):
                    raise PlanValidationError(
                        f"Illegal capability scope: path '{path_str}' "
                        "violates C:\\AEGIS isolation boundary."
                    )
            except (ValueError, RuntimeError) as exc:
                if isinstance(exc, PlanValidationError):
                    raise

    def _parse_json(self, content: str) -> Dict[str, Any]:
        """Cleanly extract and parse JSON object from model response without regex."""
        text = content.strip()
        # Strip markdown json code block fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise PlanValidationError(f"Expected JSON object, got {type(data).__name__}")
            return data
        except json.JSONDecodeError as exc:
            raise PlanValidationError(f"Invalid JSON in model output: {exc}") from exc

    def _build_system_prompt(self) -> str:
        return (
            "You are the SAGE Core Planning Agent.\n"
            "Your task is to decompose the user's goal into an ordered, "
            "minimal, machine-checkable execution plan.\n"
            "Rules:\n"
            "1. Output ONLY a valid JSON object matching the Plan schema.\n"
            "2. Use only the provided tools.\n"
            "3. Every step must have step_number (1-indexed), description, "
            "tool, arguments, is_idempotent, "
            "acceptance_criteria, estimated_risk, and target_resource.\n"
            "4. Never output markdown prose or explanations outside the JSON object.\n"
        )

    def _build_planning_prompt(self, context: ContextIR, broker: ToolBroker) -> str:
        tools_summary = []
        for name, tool_def in broker.list_tools().items():
            param_docs = []
            for p in tool_def.parameters:
                req_str = "required" if p.required else "optional"
                param_docs.append(
                    f"    - {p.name} ({p.param_type.value}, {req_str}): {p.description}"
                )
            params_str = "\n".join(param_docs) if param_docs else "    (no parameters)"
            tools_summary.append(
                f"- Tool: {name}\n"
                f"  Description: {tool_def.description}\n"
                f"  Risk: {tool_def.risk.value}\n"
                f"  Parameters:\n{params_str}"
            )

        tools_catalog = "\n".join(tools_summary)

        return (
            f"GOAL:\n{context.active_goal}\n\n"
            f"CONSTRAINTS:\n"
            + ("\n".join(f"- {c}" for c in context.constraints) if context.constraints else "None")
            + f"\n\nAVAILABLE TOOLS:\n{tools_catalog}\n\n"
            "Produce the execution plan as a JSON object with this exact structure:\n"
            "{\n"
            '  "goal": "<user goal>",\n'
            '  "rationale": "<brief rationale>",\n'
            '  "steps": [\n'
            "    {\n"
            '      "step_number": 1,\n'
            '      "description": "<step description>",\n'
            '      "tool": "<tool_name>",\n'
            '      "arguments": { ... },\n'
            '      "is_idempotent": true|false,\n'
            '      "acceptance_criteria": "<observable check>",\n'
            '      "estimated_risk": "LOW"|"MEDIUM"|"HIGH"|"CRITICAL",\n'
            '      "target_resource": "<target path or resource>"\n'
            "    }\n"
            "  ]\n"
            "}"
        )

    def _build_repair_prompt(
        self,
        goal: str,
        failed_output: str,
        error_message: str,
        broker: ToolBroker,
    ) -> str:
        tools_list = ", ".join(broker.list_tools().keys())
        return (
            f"Your previous plan output was REJECTED due to validation errors.\n"
            f"ERROR: {error_message}\n\n"
            f"ORIGINAL GOAL: {goal}\n\n"
            f"ALLOWED TOOLS: {tools_list}\n\n"
            f"PREVIOUS INVALID OUTPUT:\n{failed_output}\n\n"
            "Fix the errors and return ONLY a valid JSON object matching the required schema."
        )
