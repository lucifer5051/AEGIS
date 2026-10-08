"""SAGE Skills Subsystem -- Skill Execution & Bound Tool Gateway.

Enforces the security boundary between Skills and Tools:
- Skills can ONLY access tools explicitly listed in their manifest's tool_bindings.
- All tool execution must route through Tool Broker (which routes through Trust Core).
- Skills cannot bypass Tool Broker or Trust Core.
- Skills cannot grant themselves capabilities.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional, Sequence

from src.skills.errors import (
    SkillToolAccessDeniedError,
)
from src.skills.models import (
    SkillExecutionStatus,
    SkillInvocation,
    SkillManifest,
    SkillResult,
    VerificationStatus,
)
from src.tools.broker import ToolBroker
from src.tools.models import ToolResult
from src.trust.capability import ResourceScope

logger = logging.getLogger(__name__)


class BoundSkillToolBroker:
    """Scoped proxy wrapping ToolBroker for a specific executing skill.

    Enforces that the skill only invokes tools explicitly declared in its
    manifest's `tool_bindings`. All allowed invocations delegate to Tool Broker,
    which in turn enforces Trust Core capability authorization.
    """

    def __init__(
        self,
        broker: ToolBroker,
        manifest: SkillManifest,
        actor: str,
        project_id: Optional[str] = None,
    ) -> None:
        self._broker = broker
        self._manifest = manifest
        self._allowed_tools: frozenset[str] = frozenset(manifest.tool_bindings)
        self._actor = actor
        self._project_id = project_id

    @property
    def allowed_tools(self) -> frozenset[str]:
        """Set of tool names declared in the skill manifest."""
        return self._allowed_tools

    def invoke(
        self,
        tool_name: str,
        args: Dict[str, Any],
        scope_override: Optional[ResourceScope] = None,
        ttl_seconds: float = 60.0,
    ) -> ToolResult:
        """Invoke a tool on behalf of the executing skill.

        Raises:
            SkillToolAccessDeniedError: If tool_name is not in manifest.tool_bindings.
        """
        if tool_name not in self._allowed_tools:
            raise SkillToolAccessDeniedError(
                f"Skill '{self._manifest.skill_id}' is not authorized to invoke "
                f"tool '{tool_name}'. Allowed tool bindings: {sorted(self._allowed_tools)}"
            )

        return self._broker.invoke(
            tool_name=tool_name,
            args=args,
            actor=self._actor,
            project_id=self._project_id,
            scope_override=scope_override,
            ttl_seconds=ttl_seconds,
        )


class SkillExecutor(ABC):
    """Abstract base class for executing a SAGE skill."""

    @abstractmethod
    def execute(
        self,
        invocation: SkillInvocation,
        tool_broker: BoundSkillToolBroker,
        context: Optional[Any] = None,
    ) -> SkillResult:
        """Execute the skill according to its execution contract."""
        raise NotImplementedError


class DeterministicSkillExecutor(SkillExecutor):
    """Executor for deterministic Python callable skills."""

    def __init__(
        self,
        handler: Callable[[Dict[str, Any], BoundSkillToolBroker], Dict[str, Any]],
        expected_output_keys: Optional[Sequence[str]] = None,
    ) -> None:
        self._handler = handler
        self._expected_output_keys = expected_output_keys

    def execute(
        self,
        invocation: SkillInvocation,
        tool_broker: BoundSkillToolBroker,
        context: Optional[Any] = None,
    ) -> SkillResult:
        """Execute the deterministic callable handler within resource bounds."""
        t_start = time.perf_counter()

        try:
            output = self._handler(invocation.input, tool_broker)
            duration_ms = (time.perf_counter() - t_start) * 1000.0

            # Deterministic schema check if keys expected
            verification_status = VerificationStatus.VERIFIED
            if self._expected_output_keys:
                missing = [k for k in self._expected_output_keys if k not in output]
                if missing:
                    verification_status = VerificationStatus.FAILED
                    return SkillResult(
                        invocation_id=invocation.invocation_id,
                        skill_id=invocation.skill_id,
                        status=SkillExecutionStatus.FAILED,
                        output=output,
                        verification_status=verification_status,
                        error=f"Output verification failed: missing keys {missing}",
                        metrics={"duration_ms": duration_ms},
                    )

            return SkillResult(
                invocation_id=invocation.invocation_id,
                skill_id=invocation.skill_id,
                status=SkillExecutionStatus.SUCCESS,
                output=output,
                verification_status=verification_status,
                metrics={"duration_ms": duration_ms},
            )

        except SkillToolAccessDeniedError:
            # Propagate security violation
            raise

        except Exception as exc:
            duration_ms = (time.perf_counter() - t_start) * 1000.0
            logger.warning("Execution error in skill '%s': %s", invocation.skill_id, exc)
            return SkillResult(
                invocation_id=invocation.invocation_id,
                skill_id=invocation.skill_id,
                status=SkillExecutionStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                verification_status=VerificationStatus.FAILED,
                recovery_hint="Check invocation input arguments or tool availability.",
                metrics={"duration_ms": duration_ms},
            )
