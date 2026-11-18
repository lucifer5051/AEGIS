"""CentralBrain — Global intelligence, intent classification, agent selection and coordination.

Canonical Reference:
    PROMPT 080: Functional Stabilization, Conversation Repair & Model Manager
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple
import re
from uuid import uuid4

from src.brain.delegation import DelegationEngine, DelegationSecurityError
from src.brain.models import (
    AgentProfile,
    BrainIntent,
    DelegationRecord,
    DelegationStatus,
    IntentClassification,
)
from src.brain.registry import AgentRegistry
from src.brain.whatsapp_router import WhatsAppContactRouter
from src.models.domain import ModelRequest, TaskCategory

logger = logging.getLogger(__name__)


class CentralBrain:
    """The central intelligence and coordination layer for SAGE."""

    def __init__(
        self,
        agent_registry: AgentRegistry,
        model_gateway: Any,
        supervisor: Optional[Any] = None,
        memory_engine: Optional[Any] = None,
        trust_authority: Optional[Any] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._registry = agent_registry
        self._gateway = model_gateway
        self._supervisor = supervisor
        self._memory = memory_engine
        self._trust = trust_authority
        self._delegation_engine = DelegationEngine()
        self._whatsapp_router = WhatsAppContactRouter(
            model_gateway=self._gateway,
            central_brain=self,
            memory_engine=self._memory,
        )

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def delegation_engine(self) -> DelegationEngine:
        return self._delegation_engine

    @property
    def whatsapp_router(self) -> WhatsAppContactRouter:
        return self._whatsapp_router

    # -------------------------------------------------------------------------
    # Intent Classification & Routing
    # -------------------------------------------------------------------------

    def classify_intent(self, text, context=None):
        """High-precision classification separating conversation from task execution."""
        clean = text.strip()
        lower = clean.lower()

        # 1. Status Queries: highest priority, precede broad explanatory matching
        if any(pat in lower for pat in [
            "what are you doing", "current task", "task status",
            "show status", "what are you working on",
        ]):
            return IntentClassification(
                intent=BrainIntent.STATUS_QUERY,
                target_agent_id="friday",
                rationale="Request to inspect active supervisor execution status.",
            )

        # 2. Cancellation Commands
        if any(pat in lower for pat in [
            "cancel task", "abort task", "stop task",
            "halt execution", "cancel execution",
        ]):
            return IntentClassification(
                intent=BrainIntent.TASK_CANCEL,
                target_agent_id="friday",
                rationale="Explicit execution cancellation command.",
            )

        # 3. Conversational Pleasantries & Greetings
        _greetings = (
            "hi", "hello", "hey", "greetings", "good morning", "good afternoon",
            "good evening", "how are you", "who are you", "what are you", "what can you do",
            "thank you", "thanks", "bye", "goodbye",
        )
        stripped = lower.rstrip("?!., ")
        if stripped in _greetings or any(
            lower == g or lower.startswith(g + " ") or lower.startswith(g + ",") or lower.startswith(g + "!") or lower.startswith(g + "?")
            for g in _greetings
        ):
            return IntentClassification(
                intent=BrainIntent.CONVERSATIONAL,
                target_agent_id="friday",
                requires_reasoning_model=False,
                confidence=1.0,
                rationale="Conversational greeting or pleasantry; routes directly to model dialogue.",
            )

        # 4. Informational & Explanatory Queries (CONVERSATIONAL - NO TASK)
        _explanatory = (
            "explain ", "can you explain ", "what is ", "what are ", "how does ",
            "why is ", "why does ", "tell me about ", "describe ",
            "summarize the concept of ", "help me understand ", "what do you think about ",
        )
        if any(lower.startswith(p) for p in _explanatory):
            _force_task = ("and execute", "and run", "and create a file", "and save to", "and test")
            if not any(k in lower for k in _force_task):
                return IntentClassification(
                    intent=BrainIntent.CONVERSATIONAL,
                    target_agent_id="friday",
                    requires_reasoning_model=False,
                    rationale="Informational query; handled conversationally without supervisor task.",
                )

        # 5. Multi-Agent Collaborative Delegation
        if any(pat in lower for pat in ["compare and implement", "research and code", "collaborate", "multi-agent", "delegate"]):
            return IntentClassification(
                intent=BrainIntent.MULTI_AGENT_DELEGATION,
                target_agent_id="friday",
                requires_reasoning_model=True,
                extracted_goal=clean,
                rationale="Cross-disciplinary multi-agent objective.",
            )

        # 6. Desktop Automation -> Hermes/Operator
        _desktop = ("open chrome", "launch chrome", "open spotify", "open notepad", "desktop action", "click button", "inspect screen", "inspect desktop", "desktop window", "click notepad", "notepad")
        if any(k in lower for k in _desktop):
            return IntentClassification(
                intent=BrainIntent.ACTIONABLE_TASK,
                target_agent_id="operator",
                requires_reasoning_model=False,
                extracted_goal=clean,
                rationale="Desktop automation command routed to Hermes/Operator.",
            )

        # 7. Research / Knowledge -> Atlas
        _research = ("research ", "find papers on ", "literature review", "find sources", "what does research say", "find data on ", "analyze recent")
        if any(k in lower for k in _research):
            return IntentClassification(
                intent=BrainIntent.ACTIONABLE_TASK,
                target_agent_id="researcher",
                requires_reasoning_model=True,
                extracted_goal=clean,
                rationale="Research task delegated to Atlas.",
            )

        # 8. Engineering / Coding -> Hephaestus
        _code = ("write a python ", "implement ", "refactor ", "fix failing test", "debug ", "create a class ", "build a ", "create a script", "code a ", "write code")
        if any(lower.startswith(k) or k in lower for k in _code):
            return IntentClassification(
                intent=BrainIntent.ACTIONABLE_TASK,
                target_agent_id="coder",
                requires_reasoning_model=True,
                extracted_goal=clean,
                rationale="Engineering/coding task delegated to Hephaestus.",
            )

        # 9. General Task Execution -> Friday + Supervisor
        return IntentClassification(
            intent=BrainIntent.ACTIONABLE_TASK,
            target_agent_id="friday",
            requires_reasoning_model=False,
            extracted_goal=clean,
            rationale="Unclassified message treated as general task via supervisor.",
        )

    # -------------------------------------------------------------------------
    # Multi-Agent Coordination & Delegation
    # -------------------------------------------------------------------------

    def delegate(
        self,
        source_agent_id: str,
        destination_agent_id: str,
        assignment: str,
        task_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        shared_context_refs: Optional[List[str]] = None,
        expected_output: str = "",
    ) -> DelegationRecord:
        """Create, record, and execute an authorized delegation between agents."""
        src = self._registry.get_agent(source_agent_id)
        dst = self._registry.get_agent(destination_agent_id)

        if not src:
            raise ValueError(f"Source agent '{source_agent_id}' does not exist.")
        if not dst:
            raise ValueError(f"Destination agent '{destination_agent_id}' does not exist.")

        record = self._delegation_engine.create_delegation(
            source_agent_id=source_agent_id,
            destination_agent_id=destination_agent_id,
            assignment=assignment,
            task_id=task_id,
            parent_task_id=parent_task_id,
            correlation_id=correlation_id,
            shared_context_refs=shared_context_refs,
            expected_output=expected_output,
        )

        # Execute through Supervisor if available
        if self._supervisor:
            try:
                self._delegation_engine.update_status(record.delegation_id, DelegationStatus.RUNNING)
                goal_res = self._supervisor.execute_goal(assignment)
                if goal_res.success:
                    result_text = goal_res.synthesis or "Delegation completed successfully."
                    return self._delegation_engine.update_status(
                        record.delegation_id,
                        DelegationStatus.COMPLETED,
                        result=result_text,
                    )
                else:
                    return self._delegation_engine.update_status(
                        record.delegation_id,
                        DelegationStatus.FAILED,
                        error_message="Supervisor execution reported failure.",
                    )
            except Exception as exc:
                logger.exception("Delegation execution failed: %s", exc)
                return self._delegation_engine.update_status(
                    record.delegation_id,
                    DelegationStatus.FAILED,
                    error_message=str(exc),
                )
        else:
            simulated = f"Agent {dst.name} completed assignment: {assignment}"
            return self._delegation_engine.update_status(
                record.delegation_id,
                DelegationStatus.COMPLETED,
                result=simulated,
            )

    def aggregate_results(self, delegation_records: List[DelegationRecord]) -> str:
        """Reconcile and synthesize findings from multiple collaborating agents."""
        if not delegation_records:
            return "No delegation activities recorded."

        lines = ["### Multi-Agent Collaboration Findings:"]
        for d in delegation_records:
            dst = self._registry.get_agent(d.destination_agent_id)
            name = dst.name if dst else d.destination_agent_id
            status_icon = "✔" if d.status == DelegationStatus.COMPLETED else "✘"
            lines.append(f"- **{name}** ({status_icon} {d.status.value.upper()}): {d.assignment}")
            if d.result:
                lines.append(f"  > {d.result}")
            elif d.error_message:
                lines.append(f"  > *Error: {d.error_message}*")

        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # WhatsApp Escalation Handling
    # -------------------------------------------------------------------------

    def process_escalated_request(
        self,
        source: str,
        sender_id: str,
        text: str,
        memory_namespace: str,
    ) -> str:
        """Handle a complex request escalated from WhatsApp or other external channel."""
        classification = self.classify_intent(text)

        if classification.intent == BrainIntent.ACTIONABLE_TASK and self._supervisor:
            target_agent = self._registry.get_agent(classification.target_agent_id or "friday")
            res = self._supervisor.execute_goal(text)
            if res.success:
                return (
                    f"[{target_agent.name if target_agent else 'Central Brain'}] "
                    f"Completed your request:\n{res.synthesis}"
                )
            else:
                return f"Task execution could not complete successfully for: {text}"

        req = ModelRequest(
            prompt=text,
            system_prompt=(
                f"You are the Central Brain answering an escalated request from {sender_id}. "
                "Provide a structured, accurate, and authoritative technical response."
            ),
            task_category=TaskCategory.REASONING if classification.requires_reasoning_model else TaskCategory.GENERAL_CHAT,
            model_alias="reasoning" if classification.requires_reasoning_model else "balanced",
        )
        resp = self._gateway.generate(req)
        return resp.content

    # -------------------------------------------------------------------------
    # Multi-Model Routing & Skill Identification
    # -------------------------------------------------------------------------

    @staticmethod
    def parse_model_override(text: str) -> Tuple[str, Optional[str]]:
        """Extract and strip per-message `@model <target>` directive from text."""
        match = re.search(r"@model\s+([A-Za-z0-9_.\-]+)", text, re.IGNORECASE)
        if match:
            target = match.group(1).strip()
            clean_text = re.sub(r"@model\s+[A-Za-z0-9_.\-]+", "", text, flags=re.IGNORECASE).strip()
            if target.lower() == "auto":
                return clean_text, None
            return clean_text, target
        return text, None

    def get_brain_model_pool(self) -> Dict[str, Any]:
        """Return multi-candidate model pool for the Central Brain."""
        return {
            "primary": {"alias": "balanced", "preferred_model": "qwen2.5-7b-instruct", "description": "Primary general intelligence engine"},
            "reasoning_alternative": {"alias": "reasoning", "preferred_model": "deepseek-r1-distill-qwen-7b", "description": "Mathematical & scientific chain-of-thought engine"},
            "fast_alternative": {"alias": "fast", "preferred_model": "phi-4-mini", "description": "Ultra-low latency conversational assistant"},
            "local_fallback": {"alias": "local", "preferred_model": "sage-local-v1", "description": "Guaranteed resident zero-dependency offline engine"},
            "cloud_fallback": {"alias": "cloud", "preferred_model": "cloud-openai-default", "description": "High-capacity cloud gateway fallback"},
        }

    def select_model_for_request(
        self,
        text: str,
        user_override: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Intelligently determine the optimal model according to overrides, complexity, and capabilities."""
        clean_text, prompt_override = self.parse_model_override(text)
        effective_target = prompt_override or user_override

        # Explicit user selection
        if effective_target and effective_target.lower() != "auto":
            return {
                "selected_target": effective_target,
                "strategy": "EXPLICIT_USER_OVERRIDE",
                "clean_text": clean_text,
                "rationale": f"Honoring explicit user model override '{effective_target}'.",
                "is_fallback": False,
            }

        classification = self.classify_intent(clean_text)

        # Agent-preferred model alignment
        if agent_id:
            agent = self._registry.get_agent(agent_id)
            if agent and agent.model_alias:
                return {
                    "selected_target": agent.model_alias,
                    "strategy": "AGENT_PREFERRED",
                    "clean_text": clean_text,
                    "rationale": f"Selected preferred model alias '{agent.model_alias}' for agent '{agent.name}'.",
                    "is_fallback": False,
                }

        # Central Brain complexity-driven routing
        if classification.requires_reasoning_model:
            return {
                "selected_target": "reasoning",
                "strategy": "BRAIN_COMPLEXITY_ROUTED",
                "clean_text": clean_text,
                "rationale": "High-complexity research or engineering task routed to deep reasoning model pool.",
                "is_fallback": False,
            }

        return {
            "selected_target": "balanced",
            "strategy": "BRAIN_BALANCED_DEFAULT",
            "clean_text": clean_text,
            "rationale": "Standard request routed to primary balanced conversational engine.",
            "is_fallback": False,
        }

    def identify_required_skill(self, text: str) -> Optional[Dict[str, Any]]:
        """Identify which modular skill is best suited for an incoming actionable request."""
        lower = text.lower()
        if any(k in lower for k in ("verify code", "run test", "test suite", "check syntax", "pytest")):
            return {"skill_id": "dev.verify", "name": "Code Verification & Testing", "candidate_models": ["qwen2.5-coder-7b", "deepseek-coder-v2-lite", "devstral-small-24b"]}
        if any(k in lower for k in ("chrome", "notepad", "desktop", "click", "screen", "window", "launch app")):
            return {"skill_id": "desktop.automation", "name": "Desktop Application Controller", "candidate_models": ["mistral-7b-instruct-v0.3", "qwen2.5-7b-instruct", "hermes-3-llama-3.1-8b"]}
        if any(k in lower for k in ("research", "papers", "literature", "diagnose", "root cause", "analyze")):
            return {"skill_id": "research.diagnose", "name": "Diagnostic Reasoner", "candidate_models": ["deepseek-r1-distill-qwen-7b", "deepseek-r1-distill-qwen-1.5b", "qwen2.5-14b-instruct"]}
        if any(k in lower for k in ("inspect directory", "list files", "find file", "workspace", "read source")):
            return {"skill_id": "workspace.inspect", "name": "Workspace Inspector", "candidate_models": ["qwen2.5-7b-instruct", "gemma-2-9b-it", "phi-4-mini"]}
        return None
