"""SAGE Friday Communication Gateway -- Central Orchestrator.

Canonical Reference:
    PROMPT 072: SAGE Friday Communication Gateway & Secure Sidecar Protocol

Invariants:
- SAGE is the single authoritative agent.
- Supervisor is the sole task execution engine; communication layer NEVER directly executes tools.
- TrustCore remains the immutable security authority.
- Friday is SAGE's conversational personality, routing inference through ModelGateway.
- Identity and conversation isolation are strictly enforced.
- 100% offline testable with zero cloud requirements.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from src.communication.adapters.base import BaseChannelAdapter
from src.communication.domain import (
    ChannelIdentity,
    ChannelPermissionLevel,
    ChannelPlatform,
    ChannelStatus,
    CommunicationContext,
    ConversationType,
    IncomingMessage,
    MessageDeliveryResult,
    OutboundStatus,
    OutgoingMessage,
)
from src.communication.errors import (
    ChannelAuthorizationError,
    CommunicationError,
    UnauthorizedSenderError,
)
from src.communication.friday import FridayConversationHandler
from src.communication.outbound import OutboundPolicyEngine
from src.communication.security import SecurityPolicyEnforcer
from src.manager.briefing import BriefingStatus
from src.manager.models import TaskState
from src.manager.supervisor import Supervisor
from src.models.errors import redact_secrets_from_text
from src.scheduler.job_models import BackgroundJob, JobStatus
from src.scheduler.job_queue import JobQueue
from src.scheduler.worker_manager import WorkerManager
from src.trust.approval import ApprovalStore

logger = logging.getLogger(__name__)


class CommunicationGateway:
    """Central gateway orchestrating channels, security, Friday conversations, and task lifecycle."""

    def __init__(
        self,
        security_enforcer: SecurityPolicyEnforcer,
        friday_handler: FridayConversationHandler,
        outbound_policy: OutboundPolicyEngine,
        adapters: Optional[Dict[ChannelPlatform, BaseChannelAdapter]] = None,
        supervisor: Optional[Supervisor] = None,
        approval_store: Optional[ApprovalStore] = None,
        worker_manager: Optional[WorkerManager] = None,
        job_queue: Optional[JobQueue] = None,
    ) -> None:
        self._security = security_enforcer
        self._friday = friday_handler
        self._outbound = outbound_policy
        self._adapters: Dict[ChannelPlatform, BaseChannelAdapter] = dict(adapters) if adapters else {}
        self._supervisor = supervisor
        self._approval_store = approval_store
        self._worker_manager = worker_manager
        self._job_queue = job_queue

        # Mapping: job_id -> (ChannelPlatform, destination_conversation_id, sage_user_id)
        self._job_notifications: Dict[str, Tuple[ChannelPlatform, str, Optional[str]]] = {}

    def register_adapter(self, adapter: BaseChannelAdapter) -> None:
        """Register and store a channel adapter."""
        self._adapters[adapter.platform] = adapter

    def get_adapter(self, platform: ChannelPlatform) -> Optional[BaseChannelAdapter]:
        return self._adapters.get(platform)

    def initialize_all(self) -> None:
        """Initialize all registered channel adapters."""
        for adapter in self._adapters.values():
            try:
                adapter.initialize()
            except Exception as exc:
                logger.error("Failed to initialize channel %s: %s", adapter.platform.value, exc)

    def shutdown_all(self) -> None:
        """Shut down all channel adapters cleanly."""
        for adapter in self._adapters.values():
            try:
                adapter.shutdown()
            except Exception as exc:
                logger.warning("Error shutting down channel %s: %s", adapter.platform.value, exc)

    def health_check(self) -> Dict[str, ChannelStatus]:
        """Collect operational status across all adapters."""
        statuses = {}
        for platform, adapter in self._adapters.items():
            statuses[platform.value] = adapter.health_check()
        return statuses

    def poll_and_process(self) -> List[Tuple[IncomingMessage, Optional[OutgoingMessage]]]:
        """Poll incoming messages from all active channels and process them through SAGE."""
        processed: List[Tuple[IncomingMessage, Optional[OutgoingMessage]]] = []
        for adapter in self._adapters.values():
            try:
                incoming_batch = adapter.poll_inbound()
            except Exception as exc:
                logger.error("Failed polling channel %s: %s", adapter.platform.value, exc)
                continue

            for msg in incoming_batch:
                out = self.process_message(msg)
                processed.append((msg, out))
        return processed

    def process_message(self, message: IncomingMessage) -> Optional[OutgoingMessage]:
        """Process an incoming message through identity verification, routing, and dispatch."""
        try:
            # 1. Identity resolution and context authentication
            context = self._security.resolve_context(message)
        except UnauthorizedSenderError as exc:
            logger.warning("Message rejected: %s", exc)
            return None
        except Exception as exc:
            logger.error("Security resolution failure: %s", exc)
            return None

        # 2. Inbound attachment validation
        validated_attachments = []
        for att in message.attachments:
            try:
                val = self._security.validate_attachment(att)
                validated_attachments.append(val)
            except Exception as exc:
                return self._send_reply(
                    context,
                    f"Attachment rejected: {exc}",
                    reply_to_id=message.message_id,
                )

        raw_text = (message.text or "").strip()

        # 3. Route explicit command vs conversational interaction
        if raw_text.startswith("/"):
            return self._handle_command(context, message)
        else:
            return self._handle_conversation(context, message)

    def _handle_conversation(
        self, context: CommunicationContext, message: IncomingMessage
    ) -> Optional[OutgoingMessage]:
        """Route normal chat to Friday personality and ModelGateway."""
        try:
            self._security.check_permission(context, ChannelPermissionLevel.CHAT)
            reply_text = self._friday.handle_message(context, message)
            return self._send_reply(context, reply_text, reply_to_id=message.message_id)
        except ChannelAuthorizationError as exc:
            return self._send_reply(context, f"Access Denied: {exc}", reply_to_id=message.message_id)
        except Exception as exc:
            logger.error("Error in Friday conversation: %s", exc)
            return self._send_reply(
                context,
                f"Friday encountered an error processing your request: {exc}",
                reply_to_id=message.message_id,
            )

    def _handle_command(
        self, context: CommunicationContext, message: IncomingMessage
    ) -> Optional[OutgoingMessage]:
        """Route slash commands to SAGE task execution and approval lifecycle."""
        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        actor = f"{context.channel.value}:{context.user_id or context.sender_id}"

        if command == "/start" or command == "/help":
            help_text = (
                "👋 *SAGE Friday Communication Interface*\n\n"
                "• Send any message to speak with Friday.\n"
                "• `/task <goal>` - Propose a new background task\n"
                "• `/status` - Check channel and system health\n"
                "• `/confirm <task_id>` - Confirm and execute proposed task\n"
                "• `/cancel <task_id>` - Cancel an active task\n"
                "• `/approve <request_id>` - Approve a pending TrustCore action\n"
                "• `/reject <request_id>` - Reject a pending TrustCore action"
            )
            return self._send_reply(context, help_text, reply_to_id=message.message_id)

        elif command == "/status":
            statuses = self.health_check()
            status_lines = [
                f"• *{plat}:* {st.status} (recv: {st.messages_received}, sent: {st.messages_sent})"
                for plat, st in statuses.items()
            ]
            reply = "📡 *SAGE Channel Status:*\n" + "\n".join(status_lines)
            return self._send_reply(context, reply, reply_to_id=message.message_id)

        elif command == "/task":
            return self._cmd_task(context, args, actor, reply_to_id=message.message_id)

        elif command == "/confirm":
            return self._cmd_confirm(context, args, actor, reply_to_id=message.message_id)

        elif command == "/cancel" or command == "/stop":
            return self._cmd_cancel(context, args, reply_to_id=message.message_id)

        elif command == "/approve":
            return self._cmd_approve(context, args, actor, reply_to_id=message.message_id)

        elif command == "/reject":
            return self._cmd_reject(context, args, actor, reply_to_id=message.message_id)

        else:
            return self._send_reply(
                context,
                f"Unknown command '{command}'. Send /help to view supported commands.",
                reply_to_id=message.message_id,
            )

    def _cmd_task(
        self, context: CommunicationContext, goal: str, actor: str, reply_to_id: Optional[str] = None
    ) -> Optional[OutgoingMessage]:
        """Propose a new task and optionally enqueue a background job."""
        if not context.has_permission(ChannelPermissionLevel.TASK_REQUEST):
            return self._send_reply(
                context, "Access Denied: You lack TASK_REQUEST permission.", reply_to_id=reply_to_id
            )

        if not goal:
            return self._send_reply(
                context, "Please provide a goal: `/task <your objective>`", reply_to_id=reply_to_id
            )

        if not self._supervisor:
            return self._send_reply(
                context, "Task execution is unavailable: Supervisor not configured.", reply_to_id=reply_to_id
            )

        try:
            task = self._supervisor.submit_goal(goal)
            briefing = self._supervisor.get_context_briefing(task.task_id)

            # Bind background job if queue is present
            if self._job_queue is not None:
                job_id = f"job-{uuid4()}"
                bg_job = BackgroundJob(
                    job_id=job_id,
                    task_id=task.task_id,
                    goal_text=goal,
                    action_type="supervisor_execute",
                    status=JobStatus.PENDING,
                    metadata={"user_id": context.user_id, "channel": context.channel.value},
                )
                self._job_queue.enqueue_job(bg_job)
                self._job_notifications[job_id] = (
                    context.channel,
                    context.conversation_id,
                    context.user_id,
                )

            plan_summary = ""
            if briefing and briefing.proposed_plan:
                plan_summary = f"\nProposed Steps: {len(briefing.proposed_plan.steps)}"

            reply = (
                f"📝 *Task Proposed: `{task.task_id}`*\n"
                f"• *Goal:* {goal}\n"
                f"• *Status:* `{task.state.value}`{plan_summary}\n\n"
                f"👉 Reply `/confirm {task.task_id}` to execute.\n"
                f"👉 Reply `/cancel {task.task_id}` to abort."
            )
            return self._send_reply(context, reply, reply_to_id=reply_to_id)
        except Exception as exc:
            logger.error("Failed proposing task: %s", exc)
            return self._send_reply(context, f"Failed proposing task: {exc}", reply_to_id=reply_to_id)

    def _cmd_confirm(
        self, context: CommunicationContext, task_id: str, actor: str, reply_to_id: Optional[str] = None
    ) -> Optional[OutgoingMessage]:
        if not context.has_permission(ChannelPermissionLevel.TASK_REQUEST):
            return self._send_reply(context, "Access Denied: Lacks TASK_REQUEST permission.", reply_to_id=reply_to_id)

        clean_tid = task_id.strip()
        if not clean_tid:
            return self._send_reply(context, "Usage: `/confirm <task_id>`", reply_to_id=reply_to_id)

        if not self._supervisor:
            return self._send_reply(context, "Supervisor not configured.", reply_to_id=reply_to_id)

        try:
            briefing = self._supervisor.get_context_briefing(clean_tid)
            proposed_steps = []
            if briefing and briefing.status == BriefingStatus.AWAITING_CONFIRMATION:
                confirmed = self._supervisor.confirm_context_briefing(
                    clean_tid, confirmation_note=f"Confirmed via {context.channel.value} by {actor}"
                )
                if confirmed and confirmed.proposed_plan:
                    proposed_steps = confirmed.proposed_plan

            current_task = self._supervisor._store.get_task(clean_tid)
            if current_task and current_task.state == TaskState.SUBMITTED:
                self._supervisor.plan_task(clean_tid, proposed_steps)

            exec_task = self._supervisor.execute_task(clean_tid, actor=actor)
            reply = (
                f"🚀 *Task `{clean_tid}` Executed*\n"
                f"• *Outcome:* `{exec_task.state.value}`"
            )
            if exec_task.error_summary:
                reply += f"\n• *Details:* {exec_task.error_summary}"
            return self._send_reply(context, reply, reply_to_id=reply_to_id)
        except Exception as exc:
            return self._send_reply(context, f"Error confirming task `{clean_tid}`: {exc}", reply_to_id=reply_to_id)

    def _cmd_cancel(
        self, context: CommunicationContext, task_id: str, reply_to_id: Optional[str] = None
    ) -> Optional[OutgoingMessage]:
        clean_tid = task_id.strip()
        if not clean_tid or not self._supervisor:
            return self._send_reply(context, "Usage: `/cancel <task_id>`", reply_to_id=reply_to_id)

        try:
            self._supervisor.cancel_task(clean_tid, reason=f"Cancelled by user via {context.channel.value}")
            return self._send_reply(context, f"🛑 Task `{clean_tid}` cancelled.", reply_to_id=reply_to_id)
        except Exception as exc:
            return self._send_reply(context, f"Failed cancelling task: {exc}", reply_to_id=reply_to_id)

    def _cmd_approve(
        self, context: CommunicationContext, req_id: str, actor: str, reply_to_id: Optional[str] = None
    ) -> Optional[OutgoingMessage]:
        """Approve a pending TrustCore approval request."""
        # Group isolation invariant: Group chat members cannot approve actions
        if context.conversation_type == ConversationType.GROUP:
            return self._send_reply(
                context, "Security Invariant: Approvals cannot be executed in group chats.", reply_to_id=reply_to_id
            )

        if not context.has_permission(ChannelPermissionLevel.APPROVE):
            return self._send_reply(
                context, "Access Denied: You lack APPROVE permission.", reply_to_id=reply_to_id
            )

        clean_rid = req_id.strip()
        if not clean_rid:
            return self._send_reply(context, "Usage: `/approve <request_id>`", reply_to_id=reply_to_id)

        if not self._approval_store:
            return self._send_reply(context, "ApprovalStore not configured.", reply_to_id=reply_to_id)

        try:
            rec = self._approval_store.approve_request(clean_rid, actor=actor)
            return self._send_reply(
                context,
                f"✅ *Approval Request `{clean_rid}` Approved*\n"
                f"• *Action:* `{rec.action_name}`\n"
                f"• *Status:* `{rec.status}`",
                reply_to_id=reply_to_id,
            )
        except Exception as exc:
            return self._send_reply(context, f"Approval failed: {exc}", reply_to_id=reply_to_id)

    def _cmd_reject(
        self, context: CommunicationContext, req_id: str, actor: str, reply_to_id: Optional[str] = None
    ) -> Optional[OutgoingMessage]:
        if context.conversation_type == ConversationType.GROUP:
            return self._send_reply(
                context, "Security Invariant: Approvals cannot be rejected in group chats.", reply_to_id=reply_to_id
            )

        if not context.has_permission(ChannelPermissionLevel.APPROVE):
            return self._send_reply(
                context, "Access Denied: You lack APPROVE permission.", reply_to_id=reply_to_id
            )

        clean_rid = req_id.strip()
        if not clean_rid or not self._approval_store:
            return self._send_reply(context, "Usage: `/reject <request_id>`", reply_to_id=reply_to_id)

        try:
            rec = self._approval_store.reject_request(clean_rid, actor=actor)
            return self._send_reply(
                context,
                f"❌ *Approval Request `{clean_rid}` Rejected*\n• *Status:* `{rec.status}`",
                reply_to_id=reply_to_id,
            )
        except Exception as exc:
            return self._send_reply(context, f"Rejection failed: {exc}", reply_to_id=reply_to_id)

    def notify_job_completion(
        self, job_id: str, success: bool, outcome_summary: str
    ) -> Optional[MessageDeliveryResult]:
        """Dispatch task completion notification to the bound originating conversation."""
        binding = self._job_notifications.get(job_id)
        if not binding:
            logger.info("No notification binding registered for job %s", job_id)
            return None

        channel, conv_id, user_id = binding
        adapter = self.get_adapter(channel)
        if not adapter:
            return None

        status_icon = "✅" if success else "❌"
        msg_text = (
            f"{status_icon} *Background Job `{job_id}` Completed*\n"
            f"• *Status:* `{'SUCCESS' if success else 'FAILED'}`\n"
            f"• *Summary:* {outcome_summary}"
        )

        outgoing = OutgoingMessage(
            message_id=str(uuid4()),
            channel=channel,
            destination=conv_id,
            content=msg_text,
        )

        try:
            sanitized = self._outbound.validate_and_sanitize(outgoing, is_proactive=False)
            return adapter.send_message(sanitized)
        except Exception as exc:
            logger.error("Failed sending job completion notification: %s", exc)
            return None

    def _send_reply(
        self,
        context: CommunicationContext,
        content: str,
        reply_to_id: Optional[str] = None,
    ) -> Optional[OutgoingMessage]:
        """Sanitize and transmit reply message back through originating channel."""
        adapter = self.get_adapter(context.channel)
        if not adapter:
            logger.warning("No adapter found for channel %s", context.channel.value)
            return None

        outgoing = OutgoingMessage(
            message_id=str(uuid4()),
            channel=context.channel,
            destination=context.conversation_id,
            content=content,
            reply_to_message_id=reply_to_id,
        )

        try:
            sanitized = self._outbound.validate_and_sanitize(outgoing, is_proactive=False)
            adapter.send_message(sanitized)
            return sanitized
        except Exception as exc:
            logger.error("Failed to dispatch outbound reply: %s", exc)
            return None
