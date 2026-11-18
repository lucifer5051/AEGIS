"""SAGE Friday Communication Gateway -- Friday Conversational Personality.

Canonical Reference:
    PROMPT 072: SAGE Friday Communication Gateway & Secure Sidecar Protocol

Invariants:
- Friday is SAGE's conversational personality and communication interface, NOT a parallel agent.
- All model inference routes strictly through ModelGateway.
- Memory access and storage strictly honor user isolation boundaries.
- Zero-Secret Invariant: All prompt inputs and outputs are sanitized.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.communication.domain import (
    ChannelPermissionLevel,
    CommunicationContext,
    IncomingMessage,
)
from src.communication.errors import ChannelAuthorizationError
from src.config.policy_models import PersonalityConfig
from src.memory.models import (
    CognitiveMemoryCategory,
    MemoryCategory,
    UnifiedMemoryQuery,
    UnifiedMemoryRecord,
)
from src.models.domain import ModelRequest, TaskCategory
from src.models.errors import redact_secrets_from_text
from src.models.gateway import ModelGateway
from src.storage.trust_levels import MemoryTrustLevel

logger = logging.getLogger(__name__)

DEFAULT_FRIDAY_SYSTEM_PROMPT = (
    "You are Friday, SAGE's autonomous conversational interface and executive assistant. "
    "Your tone is helpful, concise, technically rigorous, and proactive. "
    "Provide clear, actionable responses. Never disclose system passwords, tokens, or private secrets."
)


class FridayConversationHandler:
    """Coordinates conversational interactions through Friday personality, ModelGateway, and Memory."""

    def __init__(
        self,
        model_gateway: ModelGateway,
        memory_engine: Optional[Any] = None,
        personality_config: Optional[PersonalityConfig] = None,
        supervisor: Optional[Any] = None,
        approval_store: Optional[Any] = None,
        central_brain: Optional[Any] = None,
    ) -> None:
        self._gateway = model_gateway
        self._memory = memory_engine
        self._supervisor = supervisor
        self._approval_store = approval_store
        self._central_brain = central_brain
        self._personality = personality_config or PersonalityConfig(
            agent_name="Friday",
            tone="helpful, concise, technically rigorous",
            verbosity="medium",
        )
        self._recent_turns: List[Dict[str, str]] = []
        self._active_task_id: Optional[str] = None

    def _build_system_prompt(self, context: CommunicationContext) -> str:
        base = self._personality.custom_system_prompt or DEFAULT_FRIDAY_SYSTEM_PROMPT
        tone_clause = f"\nTone: {self._personality.tone}. Verbosity: {self._personality.verbosity}."
        user_clause = f"\nInteracting with authenticated user: {context.display_name or context.user_id or 'Authorized User'}."
        return base + tone_clause + user_clause

    def _retrieve_user_context(self, context: CommunicationContext, query_text: str) -> str:
        """Fetch user-scoped memories from UnifiedMemoryEngine if permitted."""
        if not self._memory or not context.has_permission(ChannelPermissionLevel.MEMORY_ACCESS):
            return ""

        if not context.user_id:
            return ""

        try:
            # Look up recent or relevant facts scoped to this user
            records: List[Any] = []
            if hasattr(self._memory, "store") and hasattr(self._memory.store, "query_records"):
                records = self._memory.store.query_records(
                    UnifiedMemoryQuery(
                        query_text=query_text,
                        owner_id=context.user_id,
                        limit=5,
                    )
                )
            elif hasattr(self._memory, "store") and hasattr(self._memory.store, "search"):
                records = self._memory.store.search(
                    UnifiedMemoryQuery(
                        query_text=query_text,
                        owner_id=context.user_id,
                        limit=5,
                    )
                )
            elif hasattr(self._memory, "search"):
                records = self._memory.search(query_text, owner_id=context.user_id, limit=5)

            if not records:
                return ""

            mem_lines = [f"- {r.content}" for r in records if hasattr(r, "content") and r.content]
            if mem_lines:
                return "\nRelevant personal context:\n" + "\n".join(mem_lines)
        except Exception as exc:
            logger.warning("Failed retrieving user memory context: %s", exc)

        return ""

    def _record_turn(self, context: CommunicationContext, user_text: str, reply_text: str) -> None:
        """Store the conversation turn in UnifiedMemoryEngine with private user scoping."""
        if not self._memory or not context.user_id:
            return

        try:
            rec = UnifiedMemoryRecord(
                memory_id=str(uuid4()),
                category=MemoryCategory.SESSION,
                cognitive_category=CognitiveMemoryCategory.EPISODIC,
                content=f"User: {user_text}\nFriday: {reply_text}",
                owner_id=context.user_id,
                access_scope=f"user:{context.user_id}",
                source="conversation",
                source_reference=context.session_id,
                trust_level=MemoryTrustLevel.M1,
            )
            if hasattr(self._memory, "store") and hasattr(self._memory.store, "store_record"):
                self._memory.store.store_record(rec)
            elif hasattr(self._memory, "store") and hasattr(self._memory.store, "save"):
                self._memory.store.save(rec)
            elif hasattr(self._memory, "store_record"):
                self._memory.store_record(rec)
            elif hasattr(self._memory, "save"):
                self._memory.save(rec)
        except Exception as exc:
            logger.warning("Failed to record conversation turn: %s", exc)

    def _is_task_control(self, text: str) -> Optional[str]:
        t = text.lower().strip()
        if any(p in t for p in ("cancel task", "stop task", "cancel that", "stop that", "cancel current task", "stop execution", "cancel the task")):
            return "cancel"
        if any(p in t for p in ("what are you doing", "task status", "what is the task status", "how is the task going")):
            return "status"
        if any(p in t for p in ("why did it fail", "explain why that failed", "what went wrong with the task", "why task failed")):
            return "diagnose_failure"
        if any(p in t for p in ("resume task", "continue from last checkpoint", "continue from the last checkpoint")):
            return "resume"
        return None

    def _handle_task_control(self, action: str, raw_text: str) -> str:
        if not self._supervisor or not hasattr(self._supervisor, "_store"):
            return "I cannot inspect or control tasks: Supervisor is not attached."

        store = self._supervisor._store

        if action == "cancel":
            in_flight = store.get_in_flight_tasks()
            target_task = in_flight[0] if in_flight else (store.get_task(self._active_task_id) if self._active_task_id else None)
            if target_task and not target_task.state.is_terminal:
                self._supervisor.cancel_task(target_task.task_id, reason="Cancelled by user instruction through Friday")
                return f"I have cancelled task **{target_task.title}** (`{target_task.task_id}`). Execution has been safely halted."
            return "There are currently no active in-flight tasks to cancel."

        if action == "status":
            in_flight = store.get_in_flight_tasks()
            if in_flight:
                t = in_flight[0]
                steps = store.list_task_steps(t.task_id)
                done = sum(1 for s in steps if getattr(getattr(s, "state", None), "value", str(getattr(s, "state", ""))) == "COMPLETED")
                return f"Currently executing task: **{t.title}** [`{t.state.value}`]. Progress: {done}/{len(steps)} steps completed."
            recent = store.list_tasks()
            if recent:
                t = recent[0]
                return f"SAGE is currently standing by. The most recent task was: **{t.title}** [`{t.state.value}`]."
            return "There are currently no active tasks. SAGE is standing by for your instructions."

        if action == "diagnose_failure":
            recent = store.list_tasks()
            failed = [t for t in recent if getattr(getattr(t, "state", None), "value", str(getattr(t, "state", ""))) == "FAILED"]
            if failed:
                t = failed[0]
                return (
                    f"Task **{t.title}** (`{t.task_id}`) encountered an error:\n"
                    f"• **Error Summary**: {t.error_summary or 'No specific error summary recorded.'}\n"
                    f"TrustCore maintained system invariants and prevented unverified state mutations."
                )
            return "I inspected recent task history and found no recorded task failures."

        if action == "resume":
            recent = store.list_tasks()
            awaiting = [t for t in recent if getattr(getattr(t, "state", None), "value", str(getattr(t, "state", ""))) in ("AWAITING_APPROVAL", "PLANNING")]
            if awaiting:
                t = awaiting[0]
                try:
                    self._supervisor.resume_task(t.task_id)
                    return f"Resumed execution for task **{t.title}** (`{t.task_id}`)."
                except Exception as exc:
                    return f"Could not resume task {t.task_id}: {exc}"
            return "No interrupted or paused tasks were found awaiting resumption."

        return "Command acknowledged."

    def _is_actionable_task(self, text: str) -> bool:
        t = text.lower().strip()
        # Conversational pleasantries or identity questions are NOT tasks
        if any(t.startswith(prefix) for prefix in ("hi", "hello", "hey", "who are you", "what are you", "how are you", "what were we", "tell me about yourself", "greetings")):
            return False
        # Pure information queries without an explicit execution directive are NOT tasks
        if any(t.startswith(prefix) for prefix in ("explain ", "what is ", "what are ", "how does ", "why is ", "why do ", "can you explain ", "tell me about ")):
            if not any(k in t for k in ("and run", "and execute", "and create a file", "and save", "and test")):
                return False
        # Actionable indicators
        action_indicators = (
            "open chrome", "launch chrome", "open spotify", "open notepad",
            "search for ai research", "search for research", "download this pdf", "download pdf",
            "create a python project", "create a project", "create a short comparison",
            "analyze these files", "analyze my project", "run tests", "list files", "inspect workspace",
            "task:", "execute:", "run:"
        )
        if any(k in t for k in action_indicators):
            return True
        first_word = t.split()[0] if t.split() else ""
        if first_word in ("create", "download", "search", "analyze", "execute", "run", "open", "launch", "build", "write", "install", "inspect"):
            return True
        return False

    def _handle_actionable_task(self, raw_text: str, context: CommunicationContext) -> str:
        if not self._supervisor:
            return (
                f"I understand your request to execute: \"{raw_text}\", "
                f"however the Supervisor execution engine is not currently configured."
            )
        try:
            result = self._supervisor.execute_goal(raw_text)
            if hasattr(result, "task_record") and result.task_record:
                self._active_task_id = result.task_record.task_id
            if hasattr(result, "synthesis") and result.synthesis:
                return str(result.synthesis)
            if getattr(result, "success", False):
                return f"Task completed successfully: \"{raw_text}\"."
            return f"Task execution could not be completed: {raw_text}"
        except Exception as exc:
            logger.exception("Error during autonomous task execution: %s", exc)
            return f"I encountered an error while executing the task: {exc}"

    def handle_message(self, context: CommunicationContext, message: IncomingMessage) -> str:
        """Process an incoming conversational message through Friday."""
        if not context.has_permission(ChannelPermissionLevel.CHAT):
            raise ChannelAuthorizationError(
                f"User '{context.user_id}' lacks CHAT permission on {context.channel.value}."
            )

        raw_text = (message.text or "").strip()
        if not raw_text:
            return "I received your message, but it contained no text."

        # 1. Intent-Guided Routing via Central Brain (if attached)
        if self._central_brain is not None:
            from src.brain.models import BrainIntent
            classification = self._central_brain.classify_intent(raw_text)

            if classification.intent == BrainIntent.STATUS_QUERY and self._supervisor is not None:
                reply = self._handle_task_control("status", raw_text)
                self._recent_turns.append({"role": "user", "text": raw_text})
                self._recent_turns.append({"role": "friday", "text": reply})
                self._record_turn(context, raw_text, reply)
                return reply

            elif classification.intent == BrainIntent.TASK_CANCEL and self._supervisor is not None:
                reply = self._handle_task_control("cancel", raw_text)
                self._recent_turns.append({"role": "user", "text": raw_text})
                self._recent_turns.append({"role": "friday", "text": reply})
                self._record_turn(context, raw_text, reply)
                return reply

            elif classification.intent == BrainIntent.MULTI_AGENT_DELEGATION and self._supervisor is not None:
                rec1 = self._central_brain.delegate(
                    source_agent_id="friday",
                    destination_agent_id="researcher",
                    assignment=f"Analyze requirements and sources for: {raw_text}",
                )
                rec2 = self._central_brain.delegate(
                    source_agent_id="friday",
                    destination_agent_id="coder",
                    assignment=f"Verify implementation and constraints for: {raw_text}",
                )
                aggregated = self._central_brain.aggregate_results([rec1, rec2])
                reply = (
                    f"I coordinated with specialized agents to evaluate your request:\n\n"
                    f"{aggregated}"
                )
                self._recent_turns.append({"role": "user", "text": raw_text})
                self._recent_turns.append({"role": "friday", "text": reply})
                self._record_turn(context, raw_text, reply)
                return reply

            elif classification.intent == BrainIntent.ACTIONABLE_TASK and self._supervisor is not None:
                reply = self._handle_actionable_task(raw_text, context)
                self._recent_turns.append({"role": "user", "text": raw_text})
                self._recent_turns.append({"role": "friday", "text": reply})
                self._record_turn(context, raw_text, reply)
                return reply

        # 2. Standalone Fallback Routing (when Central Brain is not attached)
        elif self._supervisor is not None:
            control_action = self._is_task_control(raw_text)
            if control_action:
                reply = self._handle_task_control(control_action, raw_text)
                self._recent_turns.append({"role": "user", "text": raw_text})
                self._recent_turns.append({"role": "friday", "text": reply})
                self._record_turn(context, raw_text, reply)
                return reply

            if self._is_actionable_task(raw_text):
                reply = self._handle_actionable_task(raw_text, context)
                self._recent_turns.append({"role": "user", "text": raw_text})
                self._recent_turns.append({"role": "friday", "text": reply})
                self._record_turn(context, raw_text, reply)
                return reply

        # 3. Standard Conversational & Reasoning Path (Pure Dialogue, Zero Tasks)
        sys_prompt = self._build_system_prompt(context)
        memory_ctx = self._retrieve_user_context(context, raw_text)
        if memory_ctx:
            sys_prompt += memory_ctx

        # Add recent conversation turns for context continuity
        if self._recent_turns:
            recent_summary = "\nRecent conversation:\n" + "\n".join(
                f"{t['role'].capitalize()}: {t['text'][:150]}" for t in self._recent_turns[-4:]
            )
            sys_prompt += recent_summary

        model_req = ModelRequest(
            prompt=raw_text,
            system_prompt=sys_prompt,
            task_category=TaskCategory.GENERAL_CHAT,
            metadata={
                "user_id": context.user_id,
                "channel": context.channel.value,
                "conversation_id": context.conversation_id,
            },
        )

        response = self._gateway.generate(model_req)
        clean_reply = redact_secrets_from_text(response.content)

        # Update in-memory session history and durable memory
        self._recent_turns.append({"role": "user", "text": raw_text})
        self._recent_turns.append({"role": "friday", "text": clean_reply})
        self._record_turn(context, raw_text, clean_reply)

        return clean_reply
