"""SAGE Remote Control Gateway -- Remote Command Router.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 3 (Channels)
    PROMPT 032: SAGE Secure Remote-Control Gateway (Telegram-First)

Invariants:
- Intelligence proposes; deterministic authority decides.
- Routes all commands through existing SAGE Supervisor, skills, and Trust Core.
- Strict command allowlist: arbitrary shell execution (/exec, etc.) is forbidden.
- Zero-secret invariant on all responses and logs.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import List, Optional, Union

from src.gateway.models import RouterResponse
from src.manager.briefing import BriefingStatus
from src.manager.models import StepPlan, TaskState
from src.manager.supervisor import Supervisor
from src.skills.executor import BoundSkillToolBroker
from src.skills.first_party import (
    DEV_VERIFY_MANIFEST,
    RESEARCH_DIAGNOSE_MANIFEST,
    WORKSPACE_INSPECT_MANIFEST,
    dev_verify_handler,
    research_diagnose_handler,
    workspace_inspect_handler,
)
from src.tools.broker import ToolBroker
from src.trust.approval import ApprovalStore
from src.trust.audit import _check_no_secrets
from src.trust.authority import TrustAuthority

logger = logging.getLogger(__name__)


class CommandRouter:
    """Parses, validates, and routes authenticated remote commands to SAGE subsystems."""

    def __init__(
        self,
        supervisor: Supervisor,
        tool_broker: Optional[ToolBroker] = None,
        trust_authority: Optional[TrustAuthority] = None,
        approval_store: Optional[ApprovalStore] = None,
        workspace_alias: str = "workspace",
        workspace_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self._supervisor = supervisor
        self._tools = tool_broker or getattr(supervisor, "_tools", None)
        self._authority = trust_authority or getattr(supervisor, "_authority", None)
        self._approval_store = approval_store
        self._workspace_alias = workspace_alias
        self._workspace_dir = Path(workspace_dir) if workspace_dir else Path.cwd()

    def route(
        self,
        raw_text: str,
        sender_id: str,
        chat_id: str,
    ) -> RouterResponse:
        """Route an authenticated incoming message to its corresponding handler."""
        _check_no_secrets(raw_text)
        text = raw_text.strip()

        if not text:
            return RouterResponse(
                response_text="Empty message received. Send /help for a list of supported commands."
            )

        # Parse command name and arguments
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args_text = parts[1].strip() if len(parts) > 1 else ""

        # Dispatch command
        if command == "/start":
            return self._handle_start(sender_id)
        elif command == "/help":
            return self._handle_help()
        elif command == "/status":
            return self._handle_status()
        elif command == "/tasks":
            return self._handle_tasks()
        elif command == "/inspect":
            return self._handle_inspect(args_text)
        elif command == "/verify":
            return self._handle_verify(args_text)
        elif command == "/diagnose":
            return self._handle_diagnose(args_text)
        elif command == "/task":
            return self._handle_task(args_text, sender_id)
        elif command == "/confirm":
            return self._handle_confirm(args_text, sender_id)
        elif command in ("/cancel", "/stop"):
            return self._handle_cancel(args_text, sender_id)
        elif command == "/approve":
            return self._handle_approve(args_text, sender_id)
        elif command in ("/reject", "/deny"):
            return self._handle_reject(args_text, sender_id)
        elif command.startswith("/"):
            return RouterResponse(
                response_text=(
                    f"Unknown or unsupported command '{command}'. "
                    "SAGE accepts only registered commands. "
                    "Arbitrary shell commands, OS scripts, or code execution "
                    "are strictly prohibited. "
                    "Send /help to view supported commands."
                )
            )
        else:
            return RouterResponse(
                response_text=(
                    "SAGE Remote Gateway: To propose a new task, send:\n"
                    "/task <your goal>\n\n"
                    "For status, send /status. For command help, send /help."
                )
            )

    # =========================================================================
    # COMMAND HANDLERS
    # =========================================================================

    def _handle_start(self, sender_id: str) -> RouterResponse:
        start_text = (
            "👋 *Welcome to SAGE Secure Remote-Control Gateway*\n\n"
            f"Authenticated as user `{sender_id}`.\n"
            "SAGE is operational in local-first, zero-secret mode.\n\n"
            "*Quick Actions:*\n"
            "• Send `/status` to check system health and autonomy tier.\n"
            "• Send `/task <goal>` to propose a new task with a Context Briefing.\n"
            "• Send `/help` for a full list of supported commands.\n\n"
            "_Note: All mutations require explicit confirmation or capability approval._"
        )
        return RouterResponse(response_text=start_text)

    def _handle_help(self) -> RouterResponse:
        help_text = (
            "🛡️ *SAGE Remote-Control Gateway (Telegram-First)*\n\n"
            "*Supported Commands:*\n"
            "• `/start` - Gateway onboarding and quick actions.\n"
            "• `/status` - Return SAGE operational state, autonomy tier, and task counts.\n"
            "• `/tasks` - Show recent tasks and their processing states.\n"
            "• `/inspect [pattern]` - Inspect workspace directory (read-only).\n"
            "• `/verify [test|lint|format|typecheck|syntax]` - Run verification check.\n"
            "• `/diagnose <error_message>` - Local-first recovery hypotheses.\n"
            "• `/task <goal>` - Submit a proposed task (generates a Context Briefing).\n"
            "• `/confirm <task_id>` - Confirm a briefing and initiate execution.\n"
            "• `/cancel <task_id>` (alias: `/stop`) - Safely cancel a task or briefing.\n"
            "• `/approve <request_id>` - Approve a pending Trust Core escalation.\n"
            "• `/reject <request_id>` (alias: `/deny`) - "
            "Reject a pending Trust Core escalation.\n\n"
            "*Security Invariants:*\n"
            "• Arbitrary shell/OS commands (/exec, powershell, bash) are forbidden.\n"
            "• File operations are strictly confined to the workspace directory.\n"
            "• Protected actions require explicit human approval via Trust Core."
        )
        return RouterResponse(response_text=help_text)

    def _handle_status(self) -> RouterResponse:
        in_flight = self._supervisor._store.get_in_flight_tasks()
        pending_approvals = []
        if self._approval_store is not None:
            pending_approvals = self._approval_store.list_pending()

        autonomy_tier = "Tier 1 (Safe Bounded)"
        if self._authority is not None and hasattr(self._authority, "_tier"):
            autonomy_tier = f"Tier {getattr(self._authority._tier, 'value', self._authority._tier)}"

        status_text = (
            "📊 *SAGE System Status*\n\n"
            f"• *Status:* Operational (Local-First Gateway Active)\n"
            f"• *Autonomy Level:* {autonomy_tier}\n"
            f"• *In-Flight Tasks:* {len(in_flight)}\n"
            f"• *Pending Escalation Approvals:* {len(pending_approvals)}\n"
            f"• *Network Boundary:* Local-Only / Enforced\n"
            f"• *Workspace Alias:* {self._workspace_alias}"
        )
        return RouterResponse(response_text=status_text)

    def _handle_tasks(self) -> RouterResponse:
        all_tasks = self._supervisor._store.list_tasks()
        # Bounded list of up to 10 latest tasks
        recent = sorted(all_tasks, key=lambda t: t.created_at, reverse=True)[:10]

        if not recent:
            return RouterResponse(
                response_text=(
                    "No tasks recorded in SAGE state store. Submit one with `/task <goal>`."
                )
            )

        lines = ["📋 *Recent SAGE Tasks:*"]
        for t in recent:
            short_id = t.task_id[:8]
            state_val = t.state.value
            title_snip = t.title[:45] + ("..." if len(t.title) > 45 else "")
            action_hint = ""
            if t.state == TaskState.AWAITING_APPROVAL:
                action_hint = " ⚠️ *(Needs /confirm)*"
            lines.append(f"• `{short_id}` [{state_val}] {title_snip}{action_hint}")

        lines.append("\n_Use `/status` for health summary or `/task <goal>` to propose work._")
        return RouterResponse(response_text="\n".join(lines))

    def _handle_inspect(self, args_text: str) -> RouterResponse:
        if self._tools is None or self._authority is None:
            return RouterResponse(response_text="Error: Tools or Trust Core unavailable.")

        bound_broker = BoundSkillToolBroker(
            self._tools,
            WORKSPACE_INSPECT_MANIFEST,
            actor="telegram",
        )
        pattern = args_text.strip() or "*"
        args = {"target_dir": str(self._workspace_dir), "max_files": 50}

        try:
            result = workspace_inspect_handler(args, bound_broker)
            raw_files = result.get("files", [])
            if pattern != "*":
                files = [f for f in raw_files if fnmatch.fnmatch(f, pattern)]
            else:
                files = raw_files
            total = len(files)
            summary = result.get("summary", "Inspection complete.")
            file_sample = "\n".join(f"  - `{f}`" for f in files[:10])
            if len(files) > 10:
                file_sample += f"\n  - ... and {len(files) - 10} more"

            resp = (
                f"🔍 *Workspace Inspection (Read-Only)*\n\n"
                f"{summary}\n\n"
                f"*Files Discovered ({min(len(files), 10)}/{total}):*\n{file_sample}"
            )
            return RouterResponse(response_text=resp)
        except Exception as exc:
            logger.error("Inspect execution error: %s", exc)
            return RouterResponse(response_text=f"Inspection failed: {exc}")

    def _handle_verify(self, args_text: str) -> RouterResponse:
        if self._tools is None or self._authority is None:
            return RouterResponse(response_text="Error: Tools or Trust Core unavailable.")

        check_type = args_text.strip().lower() or "test"
        valid_checks = ("test", "lint", "format", "typecheck", "syntax")
        if check_type not in valid_checks:
            return RouterResponse(
                response_text=(
                    f"Invalid check type '{check_type}'. "
                    f"Permitted verification checks: {', '.join(valid_checks)}."
                )
            )

        bound_broker = BoundSkillToolBroker(
            self._tools,
            DEV_VERIFY_MANIFEST,
            actor="telegram",
        )
        try:
            result = dev_verify_handler(
                {"check_type": check_type, "target_path": str(self._workspace_dir)},
                bound_broker,
            )
            passed = result.get("verification_passed", False)
            icon = "✅" if passed else "❌"
            exit_code = result.get("exit_code", 1)
            output_snippet = str(result.get("output", ""))[:500]

            resp = (
                f"{icon} *Verification Check: `{check_type}`*\n"
                f"• *Passed:* {passed}\n"
                f"• *Exit Code:* {exit_code}\n\n"
                f"```\n{output_snippet}\n```"
            )
            return RouterResponse(response_text=resp)
        except Exception as exc:
            logger.error("Verification execution error: %s", exc)
            return RouterResponse(response_text=f"Verification failed: {exc}")

    def _handle_diagnose(self, args_text: str) -> RouterResponse:
        if not args_text.strip():
            return RouterResponse(
                response_text=(
                    "Please provide an error message to diagnose. "
                    "Example: `/diagnose FileNotFoundError: foo`"
                )
            )

        if self._tools is None or self._authority is None:
            return RouterResponse(response_text="Error: Tools or Trust Core unavailable.")

        bound_broker = BoundSkillToolBroker(
            self._tools,
            RESEARCH_DIAGNOSE_MANIFEST,
            actor="telegram",
        )
        try:
            result = research_diagnose_handler(
                {"error_message": args_text.strip(), "allow_online": False},
                bound_broker,
            )
            diagnosis = result.get("diagnosis", "Diagnosis complete.")
            rec = result.get("recommended_action", "No specific action formulated.")
            hypotheses = result.get("recovery_hypotheses", [])
            hypo_text = ""
            if hypotheses:
                hypo_text = "\n*Formulated Hypotheses:*\n" + "\n".join(
                    f"• [{h.get('hypothesis_id')}] {h.get('description')}" for h in hypotheses[:3]
                )

            resp = (
                f"🩺 *Error Diagnosis (Local-First)*\n\n"
                f"• *Findings:* {diagnosis}\n"
                f"• *Recommended Action:* {rec}"
                f"{hypo_text}"
            )
            return RouterResponse(response_text=resp)
        except Exception as exc:
            logger.error("Diagnosis error: %s", exc)
            return RouterResponse(response_text=f"Diagnosis failed: {exc}")

    def _handle_task(self, goal_text: str, sender_id: str) -> RouterResponse:
        if not goal_text.strip():
            return RouterResponse(
                response_text=(
                    "Please specify a task goal. Example: `/task verify development checks`"
                )
            )

        # 1. Submit goal to Supervisor
        task = self._supervisor.submit_goal(
            goal_text=goal_text.strip(),
            metadata={"originating_channel": "telegram", "sender_id": sender_id},
        )

        # Propose initial deterministic steps for context understanding briefing
        proposed_steps: List[StepPlan] = []
        lower_goal = goal_text.strip().lower()
        verify_keywords = ("verify", "check", "test", "lint", "quality", "validation")
        if any(w in lower_goal for w in verify_keywords):
            proposed_steps.append(
                StepPlan(
                    action_name="dev.verify",
                    tool_name="dev_runner",
                    is_idempotent=True,
                    arguments={"verification_type": "syntax", "code": "print('ok')"},
                )
            )
        elif any(w in lower_goal for w in ("diagnose", "error", "trace", "debug")):
            proposed_steps.append(
                StepPlan(
                    action_name="research.diagnose",
                    tool_name="research_query",
                    is_idempotent=True,
                    arguments={"query": goal_text.strip()},
                )
            )
        else:
            proposed_steps.append(
                StepPlan(
                    action_name="workspace.inspect",
                    tool_name="file_list",
                    is_idempotent=True,
                    arguments={"directory": str(self._workspace_dir)},
                )
            )

        # 2. Build Context Understanding Briefing
        briefing = self._supervisor.build_context_briefing(
            task.task_id,
            proposed_steps=proposed_steps,
        )

        # Render briefing for Telegram user
        plan_summary = ""
        if briefing.proposed_plan:
            plan_summary = "\n*Proposed Plan:*\n" + "\n".join(
                f"{i}. `{step.action_name}`" for i, step in enumerate(briefing.proposed_plan, 1)
            )

        clarifications = ""
        if briefing.clarification_questions:
            clarifications = "\n⚠️ *Clarifications Needed:*\n" + "\n".join(
                f"• {q}" for q in briefing.clarification_questions
            )

        resp = (
            f"📝 *SAGE Task Understanding & Confirmation Briefing*\n\n"
            f"• *Task ID:* `{task.task_id}`\n"
            f"• *Status:* {briefing.status.value}\n"
            f"• *Goal:* {briefing.goal}\n"
            f"{plan_summary}"
            f"{clarifications}\n\n"
            f"👉 To confirm and start execution, reply:\n"
            f"`/confirm {task.task_id}`\n\n"
            f"👉 To cancel, reply:\n"
            f"`/cancel {task.task_id}`"
        )
        return RouterResponse(
            response_text=resp,
            task_id=task.task_id,
            awaiting_approval=True,
            completed=False,
        )

    def _handle_confirm(self, args_text: str, sender_id: str) -> RouterResponse:
        task_id = self._resolve_task_id(args_text)
        if not task_id:
            return RouterResponse(
                response_text="Please provide a valid task ID to confirm: `/confirm <task_id>`"
            )

        task = self._supervisor._store.get_task(task_id)
        if not task:
            return RouterResponse(response_text=f"Task `{task_id}` not found.")

        try:
            # Confirm briefing
            confirmed = self._supervisor.confirm_context_briefing(
                task_id,
                confirmation_note=f"Confirmed via Telegram by user {sender_id}.",
            )

            # If task is in SUBMITTED state and has proposed steps, plan it before executing
            current_task = self._supervisor._store.get_task(task_id)
            if (
                current_task
                and current_task.state == TaskState.SUBMITTED
                and confirmed.proposed_plan
            ):
                self._supervisor.plan_task(task_id, confirmed.proposed_plan)

            # Initiate execution
            exec_task = self._supervisor.execute_task(task_id, actor=f"telegram:{sender_id}")

            status_msg = (
                f"🚀 *Task `{task_id}` Confirmed & Executed*\n\n"
                f"• *Goal:* {task.title}\n"
                f"• *Outcome State:* `{exec_task.state.value}`"
            )
            if exec_task.error_summary:
                status_msg += f"\n• *Details:* {exec_task.error_summary}"

            return RouterResponse(
                response_text=status_msg,
                task_id=task_id,
                awaiting_approval=exec_task.state == TaskState.AWAITING_APPROVAL,
                completed=exec_task.state.is_terminal,
            )
        except Exception as exc:
            logger.error("Error confirming task %s: %s", task_id, exc)
            return RouterResponse(
                response_text=f"Failed to confirm task `{task_id}`: {exc}",
                task_id=task_id,
            )

    def _handle_cancel(self, args_text: str, sender_id: str) -> RouterResponse:
        task_id = self._resolve_task_id(args_text)
        if not task_id:
            return RouterResponse(
                response_text="Please provide a valid task ID to cancel: `/stop <task_id>`"
            )

        task = self._supervisor._store.get_task(task_id)
        if not task:
            return RouterResponse(response_text=f"Task `{task_id}` not found.")

        if task.state.is_terminal:
            return RouterResponse(
                response_text=f"Task `{task_id}` is already in terminal state `{task.state.value}`."
            )

        try:
            briefing = self._supervisor.get_context_briefing(task_id)
            if briefing and briefing.status == BriefingStatus.AWAITING_CONFIRMATION:
                self._supervisor.cancel_context_briefing(
                    task_id, reason=f"Cancelled via Telegram by user {sender_id}."
                )
            else:
                self._supervisor.cancel_task(
                    task_id, reason=f"Cancelled via Telegram by user {sender_id}."
                )

            return RouterResponse(
                response_text=f"🛑 Task `{task_id}` has been safely cancelled.",
                task_id=task_id,
                completed=True,
            )
        except Exception as exc:
            return RouterResponse(response_text=f"Failed to cancel task `{task_id}`: {exc}")

    def _handle_approve(self, args_text: str, sender_id: str) -> RouterResponse:
        req_id = args_text.strip()
        if not req_id:
            return RouterResponse(
                response_text="Please provide the approval request ID: `/approve <request_id>`"
            )

        if self._approval_store is None:
            return RouterResponse(
                response_text="Trust Core ApprovalStore is not configured on this node."
            )

        try:
            record = self._approval_store.approve_request(req_id, actor=f"telegram:{sender_id}")
            return RouterResponse(
                response_text=(
                    f"✅ *Approval Request `{req_id[:8]}` Approved*\n\n"
                    f"• *Action:* `{record.action_name}`\n"
                    f"• *Risk Tier:* `{record.action_risk_level}`\n"
                    f"• *Status:* `{record.status}`"
                )
            )
        except Exception as exc:
            return RouterResponse(response_text=f"Failed to approve request `{req_id}`: {exc}")

    def _handle_reject(self, args_text: str, sender_id: str) -> RouterResponse:
        req_id = args_text.strip()
        if not req_id:
            return RouterResponse(
                response_text="Please provide the approval request ID: `/reject <request_id>`"
            )

        if self._approval_store is None:
            return RouterResponse(
                response_text="Trust Core ApprovalStore is not configured on this node."
            )

        try:
            record = self._approval_store.reject_request(
                req_id, actor=f"telegram:{sender_id}", reason="Rejected via Telegram"
            )
            return RouterResponse(
                response_text=(
                    f"❌ *Approval Request `{req_id[:8]}` Rejected*\n\n"
                    f"• *Action:* `{record.action_name}`\n"
                    f"• *Status:* `{record.status}`"
                )
            )
        except Exception as exc:
            return RouterResponse(response_text=f"Failed to reject request `{req_id}`: {exc}")

    def _resolve_task_id(self, arg: str) -> Optional[str]:
        target = arg.strip()
        if not target:
            return None
        # Exact match
        task = self._supervisor._store.get_task(target)
        if task:
            return task.task_id
        # Prefix match (e.g. 8-char short ID)
        all_tasks = self._supervisor._store.list_tasks()
        matches = [t.task_id for t in all_tasks if t.task_id.startswith(target)]
        if len(matches) == 1:
            return matches[0]
        return None
