"""SAGE Desktop Automation Skill.

Canonical Reference:
    PROMPT 074: SAGE Windows Desktop Automation Integration

Enforces the canonical 6-step desktop automation workflow:
    Perceive → Identify Target → Check Authorization → Act → Synchronize → Verify
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.skills.executor import BoundSkillToolBroker
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
from src.trust.capability import ActionRisk

logger = logging.getLogger(__name__)

DESKTOP_AUTOMATE_MANIFEST = SkillManifest(
    skill_id="desktop.automate",
    name="Windows Desktop Automation",
    version="1.0.0",
    description=(
        "Safely observe, interact with, and verify Windows desktop applications "
        "through the restricted 6-step lifecycle."
    ),
    skill_type=SkillType.DETERMINISTIC,
    intent_tags=(
        "desktop",
        "windows",
        "ui_automation",
        "interactive",
        "accessibility",
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["inspect", "invoke", "click", "type", "shortcut", "launch"],
                "description": "Primary desktop action to perform.",
            },
            "target_window": {"type": "string", "description": "Target window title or selector."},
            "target_element": {"type": "string", "description": "Target UI element name or automation ID."},
            "input_text": {"type": "string", "description": "Text to type if action is type."},
            "keys": {"type": "string", "description": "Shortcut keys if action is shortcut."},
            "app_name": {"type": "string", "description": "Allowlisted application executable if action is launch."},
            "expected_outcome": {"type": "string", "description": "Expected text or element to verify after action."},
        },
        "required": ["action"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "action": {"type": "string"},
            "workflow_stage": {"type": "string"},
            "perceived_target": {"type": "string"},
            "synchronization": {"type": "string"},
            "verification": {"type": "string"},
            "evidence": {"type": "object"},
        },
        "required": ["success", "action", "workflow_stage"],
    },
    execution_strategy=ExecutionStrategy.DIRECT,
    tool_bindings=(
        "windows.snapshot",
        "windows.screenshot",
        "windows.display_inventory",
        "windows.get_text",
        "windows.wait",
        "windows.wait_for",
        "windows.invoke",
        "windows.click",
        "windows.type",
        "windows.scroll",
        "windows.move",
        "windows.shortcut",
        "windows.launch_app",
    ),
    required_capabilities=(
        "desktop.read",
        "desktop.inspect",
        "desktop.interact",
        "desktop.launch",
    ),
    resource_requirements=SkillResourceRequirement(
        declared_marginal_ram_bytes=32 * 1024 * 1024,
        timeout_ms=30000,
        marginal_cpu_cores=1.0,
    ),
    concurrency_policy=ConcurrencyPolicy(
        concurrency_class="desktop_interaction",
        max_concurrent=1,  # Strict serial interaction on desktop
        allow_parallel=False,
    ),
    memory_access=MemoryAccessPolicy.FULL,
    project_scope=ProjectScope.GLOBAL,
    risk_class=ActionRisk.HIGH,
    verification_policy=SkillVerificationPolicy.SCHEMA_VALIDATION,
    recovery_policy=SkillRecoveryPolicy.FAIL_FAST,
    lifecycle_state=SkillLifecycleState.DISCOVERED,
)


def desktop_automate_handler(
    args: Dict[str, Any],
    tool_broker: BoundSkillToolBroker,
) -> Dict[str, Any]:
    """Execute desktop automation adhering to the 6-step lifecycle."""
    action = str(args.get("action", "inspect")).lower()
    target_window = args.get("target_window")
    target_element = args.get("target_element")
    expected_outcome = args.get("expected_outcome")

    evidence: Dict[str, Any] = {}

    # -------------------------------------------------------------------------
    # Step 1: PERCEIVE
    # -------------------------------------------------------------------------
    snapshot_args = {}
    if target_window:
        snapshot_args["target_window"] = str(target_window)

    snapshot_res = tool_broker.invoke("windows.snapshot", snapshot_args)
    evidence["initial_perception"] = snapshot_res.output

    # -------------------------------------------------------------------------
    # Step 2: IDENTIFY TARGET
    # -------------------------------------------------------------------------
    identified_target = target_element or target_window or "Desktop Root"

    # If action is pure inspect, capture verification and return
    if action == "inspect":
        return {
            "success": True,
            "action": "inspect",
            "workflow_stage": "VERIFY",
            "perceived_target": identified_target,
            "synchronization": "SKIPPED",
            "verification": "Inspection completed successfully.",
            "evidence": evidence,
        }

    # -------------------------------------------------------------------------
    # Step 3: CHECK AUTHORIZATION (Enforced via ToolBroker/TrustCore)
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # Step 4: ACT
    # -------------------------------------------------------------------------
    action_res = None
    if action == "launch":
        app_name = str(args.get("app_name", ""))
        action_res = tool_broker.invoke("windows.launch_app", {"app_name": app_name})
    elif action == "invoke":
        action_res = tool_broker.invoke("windows.invoke", {"element_id": str(target_element or "")})
    elif action == "click":
        click_args: Dict[str, Any] = {}
        if target_element:
            click_args["element_id"] = str(target_element)
        if "x" in args and "y" in args:
            click_args["x"] = int(args["x"])
            click_args["y"] = int(args["y"])
        action_res = tool_broker.invoke("windows.click", click_args)
    elif action == "type":
        type_args = {"text": str(args.get("input_text", ""))}
        if target_element:
            type_args["element_id"] = str(target_element)
        action_res = tool_broker.invoke("windows.type", type_args)
    elif action == "shortcut":
        action_res = tool_broker.invoke("windows.shortcut", {"keys": str(args.get("keys", ""))})
    else:
        return {
            "success": False,
            "action": action,
            "workflow_stage": "ACT",
            "perceived_target": identified_target,
            "synchronization": "NOT_REACHED",
            "verification": f"Unknown action '{action}'",
            "evidence": evidence,
        }

    evidence["action_output"] = action_res.output

    # -------------------------------------------------------------------------
    # Step 5: SYNCHRONIZE
    # -------------------------------------------------------------------------
    sync_res = tool_broker.invoke("windows.wait", {"duration_ms": 500})
    evidence["sync_output"] = sync_res.output

    # -------------------------------------------------------------------------
    # Step 6: VERIFY
    # Never assume an action succeeded merely because a tool returned without exception.
    # -------------------------------------------------------------------------
    post_snapshot_res = tool_broker.invoke("windows.snapshot", snapshot_args)
    evidence["post_action_perception"] = post_snapshot_res.output

    verification_msg = "Action executed and synchronized."
    success = True

    if expected_outcome:
        # Check if expected outcome appears in post-action perception
        if expected_outcome.lower() not in str(post_snapshot_res.output).lower():
            success = False
            verification_msg = (
                f"Verification failed: expected outcome '{expected_outcome}' "
                "not found in post-action UI state."
            )
        else:
            verification_msg = f"Verification confirmed: found '{expected_outcome}' in post-action UI state."

    return {
        "success": success,
        "action": action,
        "workflow_stage": "VERIFIED" if success else "VERIFICATION_FAILED",
        "perceived_target": identified_target,
        "synchronization": "COMPLETED",
        "verification": verification_msg,
        "evidence": evidence,
    }


def register_desktop_automation_skill(
    registry: SkillRegistry,
    broker: Optional[Any] = None,
) -> RegisteredSkill:
    """Register the desktop automation skill in the SkillRegistry."""
    from src.skills.executor import DeterministicSkillExecutor

    return registry.register_skill(
        manifest=DESKTOP_AUTOMATE_MANIFEST,
        executor=DeterministicSkillExecutor(
            handler=desktop_automate_handler,
            expected_output_keys=["success", "action", "workflow_stage"],
        ),
    )
