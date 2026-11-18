"""WhatsAppContactRouter — Scoped contact isolation and small-model routing.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & Independent Multi-Agent System
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from src.brain.models import BrainIntent, IntentClassification
from src.models.domain import ModelRequest, TaskCategory

logger = logging.getLogger(__name__)

# Heuristic patterns for routine greetings and casual replies
ROUTINE_CASUAL_PATTERNS = [
    "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
    "how are you", "who are you", "what can you do", "help", "thanks", "thank you",
    "bye", "goodbye", "ok", "okay", "yes", "no", "sure", "cool"
]

# Patterns that indicate complex or tool-requiring intent that must escalate
COMPLEX_ESCALATION_PATTERNS = [
    "run", "execute", "create", "delete", "fix", "inspect", "investigate",
    "analyze", "build", "verify", "deploy", "search files", "test", "automate",
    "workflow", "plan", "code", "benchmark", "desktop", "powershell"
]


class WhatsAppContactSession:
    """Isolated session state for a single WhatsApp contact."""

    def __init__(self, contact_id: str) -> None:
        self.contact_id = contact_id
        self.memory_namespace = f"contact:{contact_id}"
        self.history: List[Dict[str, str]] = []
        self.active_task_id: Optional[str] = None
        self.model_alias: str = "fast"  # Small/efficient model by default


class WhatsAppContactRouter:
    """Manages independent WhatsApp contact contexts, small-model routing, and escalation."""

    def __init__(
        self,
        model_gateway: Any,
        central_brain: Any,
        memory_engine: Optional[Any] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._gateway = model_gateway
        self._brain = central_brain
        self._memory = memory_engine
        self._contact_sessions: Dict[str, WhatsAppContactSession] = {}

    def get_or_create_contact_session(self, contact_id: str) -> WhatsAppContactSession:
        """Retrieve isolated contact session, preventing cross-contact leakage."""
        with self._lock:
            if contact_id not in self._contact_sessions:
                self._contact_sessions[contact_id] = WhatsAppContactSession(contact_id)
            return self._contact_sessions[contact_id]

    def evaluate_routing(self, contact_id: str, text: str) -> Tuple[bool, str]:
        """Determine whether message can be handled by small model or requires Central Brain.

        Returns:
            (is_routine_small_model: bool, rationale: str)
        """
        lower = text.strip().lower()

        # Check for complex actions first
        for pat in COMPLEX_ESCALATION_PATTERNS:
            if pat in lower:
                return (
                    False,
                    f"Message contains operational command or analysis keyword '{pat}', escalating to Central Brain."
                )

        # Check for routine casual patterns
        if lower in ROUTINE_CASUAL_PATTERNS or len(lower) < 25:
            return (
                True,
                "Message matches casual conversational or greeting profile; routed to local small model."
            )

        # Questions or general queries: if they ask about external system or code, escalate
        if any(term in lower for term in ["error", "bug", "traceback", "file", "path", "system"]):
            return (
                False,
                "Message requires technical investigation; escalating to Central Brain."
            )

        # Default for normal queries: small model handles conversational queries
        return (True, "Routine general conversation; routed to small model.")

    def handle_contact_message(
        self,
        contact_id: str,
        text: str,
        sender_name: Optional[str] = None,
    ) -> str:
        """Process WhatsApp message within isolated context using appropriate model tier."""
        session = self.get_or_create_contact_session(contact_id)
        is_routine, rationale = self.evaluate_routing(contact_id, text)

        logger.info(
            "WhatsApp routing for %s: small_model=%s, rationale=%s",
            contact_id,
            is_routine,
            rationale,
        )

        if not is_routine:
            # Escalate to Central Brain for reasoning or multi-agent delegation
            reply = self._brain.process_escalated_request(
                source="whatsapp",
                sender_id=contact_id,
                text=text,
                memory_namespace=session.memory_namespace,
            )
        else:
            # Process via small local model directly
            sys_prompt = (
                f"You are SAGE WhatsApp Assistant responding to {sender_name or 'contact'}. "
                "Keep responses polite, brief, and helpful. "
                "If asked to run commands or perform deep technical tasks, explain that you can escalate."
            )
            if session.history:
                recent = "\n".join(f"{h['role']}: {h['text']}" for h in session.history[-3:])
                sys_prompt += f"\nRecent dialogue:\n{recent}"

            req = ModelRequest(
                prompt=text,
                system_prompt=sys_prompt,
                task_category=TaskCategory.GENERAL_CHAT,
                model_alias=session.model_alias,  # "fast"
            )
            resp = self._gateway.generate(req)
            reply = resp.content

        # Update strictly isolated contact history
        with self._lock:
            session.history.append({"role": "user", "text": text})
            session.history.append({"role": "assistant", "text": reply})
            if len(session.history) > 20:
                session.history = session.history[-20:]

        return reply
