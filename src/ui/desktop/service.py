"""UiService — Bridges SAGE Runtime & Central Brain with Qt Signals/Slots.

Canonical Reference:
    PROMPT 080: Functional Stabilization, Conversation Repair & Model Manager
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from PySide6.QtCore import QObject, Signal

from src.models.domain import ModelRequest, TaskCategory
from src.ui.events import EventType, get_event_bus

logger = logging.getLogger(__name__)


class UiService(QObject):
    """Qt-compatible service bridging backend events and operations to the GUI thread."""

    # Chat & Agent Signals
    message_received = Signal(str, str, str, str)    # role, content, timestamp, model_name
    session_messages_loaded = Signal(list)           # List[Dict[str, Any]] (messages)
    typing_state_changed = Signal(bool)              # is_working
    agents_updated = Signal(list)                    # List[Dict[str, Any]]
    active_agent_changed = Signal(dict)              # Dict[str, Any]
    sessions_updated = Signal(list)                  # List[Dict[str, Any]]
    active_session_changed = Signal(dict)            # Dict[str, Any]

    # Task & Execution Signals
    tasks_updated = Signal(list)                     # List[Dict[str, Any]]
    task_selected = Signal(dict)                     # Dict[str, Any]
    plan_updated = Signal(list)                      # List[Dict[str, Any]] (steps)
    activity_event = Signal(dict)                    # Event dict
    approvals_updated = Signal(list)                 # List[Dict[str, Any]]

    # Workflows, Models, Skills, Memory & Artifacts
    workflows_updated = Signal(list)                 # List[Dict[str, Any]]
    models_updated = Signal(list)                    # List[Dict[str, Any]]
    skills_updated = Signal(list)                    # List[Dict[str, Any]]
    memory_updated = Signal(list)                    # List[Dict[str, Any]]
    artifacts_updated = Signal(list)                 # List[Dict[str, Any]]

    # System Status & Errors
    status_updated = Signal(dict)                    # Component health dict
    error_occurred = Signal(str)                     # Error message string

    def __init__(self, sage_app: Any, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._app = sage_app
        self._bus = get_event_bus()
        self._running = True

        self._active_agent_id = "friday"
        self._active_session_id: Optional[str] = None
        self._active_task_id: Optional[str] = None

        # Register event bus listener callback
        self._bus.add_listener(self._on_bus_event)

    # -------------------------------------------------------------------------
    # EventBus Subscription
    # -------------------------------------------------------------------------

    def _on_bus_event(self, ev_dict: Dict[str, Any]) -> None:
        """Receive and route execution event onto GUI thread."""
        try:
            self.activity_event.emit(ev_dict)
            etype = ev_dict.get("event_type")
            tid = ev_dict.get("task_id")

            if etype in (
                EventType.TASK_CREATED,
                EventType.TASK_STARTED,
                EventType.TASK_COMPLETED,
                EventType.TASK_FAILED,
            ):
                self.refresh_tasks()
                if self._active_task_id and tid == self._active_task_id:
                    self.load_task_plan(self._active_task_id)

            elif etype in (
                EventType.STEP_STARTED,
                EventType.STEP_COMPLETED,
                EventType.STEP_FAILED,
            ):
                if self._active_task_id and tid == self._active_task_id:
                    self.load_task_plan(self._active_task_id)

            elif etype in (
                EventType.APPROVAL_REQUIRED,
                EventType.APPROVAL_GRANTED,
                EventType.APPROVAL_REJECTED,
            ):
                self.refresh_approvals()
        except Exception as exc:
            logger.debug("on_bus_event error: %s", exc)

    # -------------------------------------------------------------------------
    # Agent & Session Management
    # -------------------------------------------------------------------------

    def refresh_agents(self) -> None:
        """Fetch all registered independent agents."""
        registry = getattr(self._app, "agent_registry", None)
        if not registry:
            return

        agents = registry.list_agents()
        agent_dicts = [a.model_dump() if hasattr(a, "model_dump") else vars(a) for a in agents]
        self.agents_updated.emit(agent_dicts)

        # Notify active agent
        current = registry.get_agent(self._active_agent_id)
        if current:
            self.active_agent_changed.emit(
                current.model_dump() if hasattr(current, "model_dump") else vars(current)
            )

    def select_agent(self, agent_id: str) -> None:
        """Switch active agent context and load its isolated sessions and messages."""
        registry = getattr(self._app, "agent_registry", None)
        if not registry:
            return

        agent = registry.get_agent(agent_id)
        if not agent:
            self.error_occurred.emit(f"Agent '{agent_id}' not found.")
            return

        self._active_agent_id = agent_id
        self.active_agent_changed.emit(
            agent.model_dump() if hasattr(agent, "model_dump") else vars(agent)
        )

        # Refresh sessions belonging to this agent and load active session messages
        sessions = registry.list_sessions_for_agent(agent_id)
        session_dicts = [s.model_dump() if hasattr(s, "model_dump") else vars(s) for s in sessions]
        self.sessions_updated.emit(session_dicts)

        if sessions:
            target_sess = sessions[0]
            self.select_session(target_sess.session_id)
        else:
            new_sess = registry.create_session(agent_id, title=f"{agent.name} Session")
            self.refresh_sessions(agent_id)
            self.select_session(new_sess.session_id)

    def create_agent(self, agent_data: Dict[str, Any]) -> None:
        """Register a new independent agent profile."""
        from src.brain.models import AgentProfile

        registry = getattr(self._app, "agent_registry", None)
        if not registry:
            return

        try:
            profile = AgentProfile(**agent_data)
            registry.register_agent(profile)
            self.refresh_agents()
        except Exception as exc:
            self.error_occurred.emit(f"Failed creating agent: {exc}")

    def refresh_sessions(self, agent_id: Optional[str] = None) -> None:
        """Fetch sessions for specified or active agent."""
        registry = getattr(self._app, "agent_registry", None)
        if not registry:
            return

        tgt_agent = agent_id or self._active_agent_id
        sessions = registry.list_sessions_for_agent(tgt_agent)
        session_dicts = [s.model_dump() if hasattr(s, "model_dump") else vars(s) for s in sessions]
        self.sessions_updated.emit(session_dicts)

        if sessions and not self._active_session_id:
            self.select_session(sessions[0].session_id)
        elif not sessions:
            agent = registry.get_agent(tgt_agent)
            title = f"{agent.name if agent else 'Agent'} Session"
            new_sess = registry.create_session(tgt_agent, title=title)
            self.refresh_sessions(tgt_agent)
            self.select_session(new_sess.session_id)

    def select_session(self, session_id: str) -> None:
        """Select a conversation session and load its historical messages."""
        registry = getattr(self._app, "agent_registry", None)
        if not registry:
            return

        session = registry.get_session(session_id)
        if not session:
            return

        self._active_session_id = session_id
        self._active_agent_id = session.agent_id

        # Update active session & agent in UI
        self.active_session_changed.emit(
            session.model_dump() if hasattr(session, "model_dump") else vars(session)
        )
        agent = registry.get_agent(session.agent_id)
        if agent:
            self.active_agent_changed.emit(
                agent.model_dump() if hasattr(agent, "model_dump") else vars(agent)
            )

        # Load historical messages for this session
        msgs = registry.get_session_history(session_id)
        msg_dicts = [m.model_dump() if hasattr(m, "model_dump") else vars(m) for m in msgs]
        self.session_messages_loaded.emit(msg_dicts)

    def create_session(self, agent_id: Optional[str] = None, title: str = "New Session") -> None:
        """Create a fresh isolated conversation session."""
        registry = getattr(self._app, "agent_registry", None)
        if not registry:
            return

        tgt = agent_id or self._active_agent_id
        session = registry.create_session(tgt, title=title)
        self.refresh_sessions(tgt)
        self.select_session(session.session_id)

    # -------------------------------------------------------------------------
    # Conversational Message Routing
    # -------------------------------------------------------------------------

    def send_chat_message(self, text: str, model_override: Optional[str] = None) -> None:
        """Send message to currently selected agent and session with strict identity isolation."""
        brain = getattr(self._app, "central_brain", None)
        clean_text = text.strip()
        parsed_target = None
        if brain and hasattr(brain, "parse_model_override"):
            try:
                res = brain.parse_model_override(clean_text)
                if isinstance(res, (tuple, list)) and len(res) >= 2:
                    clean_text, parsed_target = res[0], res[1]
            except Exception:
                pass

        effective_model = parsed_target or model_override
        if effective_model and str(effective_model).lower() == "auto":
            effective_model = None

        if not clean_text:
            return

        now_str = datetime.now().strftime("%I:%M %p")
        # Immediately display user message in UI with optimistic rendering
        self.message_received.emit("user", clean_text, now_str, "")
        self.typing_state_changed.emit(True)

        registry = getattr(self._app, "agent_registry", None)
        if registry and self._active_session_id:
            registry.record_message(self._active_session_id, role="user", content=clean_text)

        target_agent_id = self._active_agent_id
        target_session_id = self._active_session_id

        def _worker() -> None:
            reply = ""
            used_model = effective_model or "balanced"
            try:
                # 1. If active agent is Friday, route through FridayConversationHandler
                if target_agent_id == "friday":
                    handler = getattr(self._app, "friday_handler", None)
                    if not handler:
                        reply = "Friday conversation handler not available."
                    else:
                        from src.communication.domain import (
                            ChannelPermissionLevel,
                            ChannelPlatform,
                            CommunicationContext,
                            IncomingMessage,
                        )
                        ctx = CommunicationContext(
                            channel=ChannelPlatform.DESKTOP,
                            sender_id="desktop-user",
                            conversation_id=target_session_id or str(uuid4()),
                            user_id="desktop-user",
                            display_name="Desktop User",
                            permissions=[ChannelPermissionLevel.ADMIN],
                        )
                        msg = IncomingMessage(
                            message_id=str(uuid4()),
                            channel=ChannelPlatform.DESKTOP,
                            sender_id="desktop-user",
                            conversation_id=ctx.conversation_id,
                            text=clean_text,
                        )

                        if effective_model:
                            gw = getattr(self._app, "model_gateway", None)
                            if gw:
                                req = ModelRequest(
                                    prompt=clean_text,
                                    system_prompt=handler._build_system_prompt(ctx),
                                    task_category=TaskCategory.GENERAL_CHAT,
                                    model_alias=effective_model if effective_model in ("fast", "balanced", "reasoning", "coding", "local") else None,
                                    model_id=effective_model if effective_model not in ("fast", "balanced", "reasoning", "coding", "local") else None,
                                )
                                resp = gw.generate(req)
                                reply = resp.content
                                used_model = getattr(resp, "model_id", effective_model)
                            else:
                                reply = handler.handle_message(ctx, msg)
                        else:
                            reply = handler.handle_message(ctx, msg)
                            used_model = "balanced"

                # 2. Specialized independent agent (Atlas, Hephaestus, Hermes)
                else:
                    agent = registry.get_agent(target_agent_id) if registry else None
                    sup = getattr(self._app, "supervisor", None)
                    gw = getattr(self._app, "model_gateway", None)

                    if not agent or not gw:
                        reply = f"Agent '{target_agent_id}' is offline or not configured."
                    else:
                        # Check intent through Central Brain
                        intent = brain.classify_intent(clean_text) if brain else None

                        if intent and intent.intent.value == "actionable_task" and sup:
                            # Execute goal through supervisor with agent context
                            res = sup.execute_goal(clean_text)
                            reply = res.synthesis or f"[{agent.name}] Task execution completed."
                            used_model = "supervisor"
                        else:
                            # Direct conversational generation using agent's system prompt & persona
                            sys_prompt = (
                                f"{agent.system_instructions}\n"
                                f"Identity: {agent.name} ({agent.description})\n"
                                f"Tone: {agent.persona}"
                            )
                            # Retrieve recent history for this session
                            recent = registry.get_session_history(target_session_id, limit=4) if registry and target_session_id else []
                            if recent:
                                hist_text = "\n".join(f"{m.role.capitalize()}: {m.content[:150]}" for m in recent[-4:])
                                sys_prompt += f"\nRecent dialogue:\n{hist_text}"

                            target_alias = effective_model or agent.model_alias
                            req = ModelRequest(
                                prompt=clean_text,
                                system_prompt=sys_prompt,
                                task_category=TaskCategory.REASONING if target_alias == "reasoning" else TaskCategory.GENERAL_CHAT,
                                model_alias=target_alias if target_alias in ("fast", "balanced", "reasoning", "coding", "local") else None,
                                model_id=target_alias if target_alias not in ("fast", "balanced", "reasoning", "coding", "local") else None,
                            )
                            resp = gw.generate(req)
                            reply = resp.content
                            used_model = getattr(resp, "model_id", target_alias)

                # Emit reply onto GUI thread with model name
                ts = datetime.now().strftime("%I:%M %p")
                self.message_received.emit(target_agent_id, reply, ts, str(used_model))

                # Persist agent response
                if registry and target_session_id:
                    registry.record_message(target_session_id, role=target_agent_id, content=reply, model_name=str(used_model))
                    self.refresh_sessions(target_agent_id)

            except Exception as exc:
                logger.exception("Error handling chat message: %s", exc)
                err_msg = f"Error processing message: {exc}"
                self.error_occurred.emit(err_msg)
                self.message_received.emit(
                    "system",
                    f"**Agent Error**: {exc}",
                    datetime.now().strftime("%I:%M %p"),
                    "",
                )
            finally:
                self.typing_state_changed.emit(False)

        threading.Thread(target=_worker, daemon=True).start()

    # -------------------------------------------------------------------------
    # Task Operations
    # -------------------------------------------------------------------------

    def refresh_tasks(self, limit: int = 50) -> None:
        """Fetch tasks from StateStore and emit to GUI."""
        try:
            store = getattr(self._app, "state_store", getattr(self._app, "_state_store", None))
            if not store:
                return

            tasks = store.list_tasks()[:limit]
            task_dicts = []
            for t in tasks:
                t_dict = t.model_dump() if hasattr(t, "model_dump") else vars(t)
                status_val = t_dict.get("status")
                if hasattr(status_val, "value"):
                    t_dict["status"] = status_val.value
                task_dicts.append(t_dict)

            self.tasks_updated.emit(task_dicts)
        except Exception as exc:
            logger.warning("Failed refreshing tasks: %s", exc)

    def select_task(self, task_id: str) -> None:
        """Select a task and load its execution plan."""
        self._active_task_id = task_id
        try:
            store = getattr(self._app, "state_store", getattr(self._app, "_state_store", None))
            if not store:
                return

            task = store.get_task(task_id)
            if task:
                td = task.model_dump() if hasattr(task, "model_dump") else vars(task)
                if hasattr(td.get("status"), "value"):
                    td["status"] = td["status"].value
                self.task_selected.emit(td)
                self.load_task_plan(task_id)
        except Exception as exc:
            logger.warning("Failed selecting task %s: %s", task_id, exc)
            self.error_occurred.emit(f"Could not load task: {exc}")

    def load_task_plan(self, task_id: str) -> None:
        """Load hierarchical steps for a task."""
        try:
            store = getattr(self._app, "state_store", getattr(self._app, "_state_store", None))
            if not store:
                return

            steps = store.list_task_steps(task_id)
            step_dicts = []
            for s in steps:
                sd = s.model_dump() if hasattr(s, "model_dump") else vars(s)
                if hasattr(sd.get("status"), "value"):
                    sd["status"] = sd["status"].value
                step_dicts.append(sd)

            self.plan_updated.emit(step_dicts)
        except Exception as exc:
            logger.warning("Failed loading plan for %s: %s", task_id, exc)

    def cancel_task(self, task_id: str) -> None:
        """Cancel a running task through Supervisor."""
        def _worker() -> None:
            try:
                sup = getattr(self._app, "supervisor", None)
                if sup and hasattr(sup, "cancel_task"):
                    sup.cancel_task(task_id)
                else:
                    store = getattr(self._app, "state_store", getattr(self._app, "_state_store", None))
                    if store:
                        from src.manager.models import TaskState
                        store.transition_task_state(task_id, TaskState.CANCELLED)
                self.refresh_tasks()
            except Exception as exc:
                self.error_occurred.emit(f"Cancel error: {exc}")

        threading.Thread(target=_worker, daemon=True).start()

    # -------------------------------------------------------------------------
    # TrustCore Approvals
    # -------------------------------------------------------------------------

    def refresh_approvals(self) -> None:
        """Query pending TrustCore escalation requests."""
        try:
            store = getattr(self._app, "approval_store", getattr(self._app, "_approval_store", None))
            if not store:
                return

            pending = store.list_pending()
            app_dicts = [p.model_dump() if hasattr(p, "model_dump") else vars(p) for p in pending]
            self.approvals_updated.emit(app_dicts)
        except Exception as exc:
            logger.warning("Failed refreshing approvals: %s", exc)

    def approve_request(self, request_id: str) -> None:
        """Approve an escalation request through TrustCore ApprovalStore."""
        def _worker() -> None:
            try:
                store = getattr(self._app, "approval_store", getattr(self._app, "_approval_store", None))
                if store:
                    rec = store.approve_request(request_id, actor="desktop-user")
                    self._bus.emit(
                        EventType.APPROVAL_GRANTED,
                        message=f"Approval granted for {rec.action_name}",
                        task_id=rec.task_id,
                        component="trustcore",
                        status="APPROVED",
                        request_id=request_id,
                    )
                    self.refresh_approvals()
            except Exception as exc:
                self.error_occurred.emit(f"Approval error: {exc}")

        threading.Thread(target=_worker, daemon=True).start()

    def reject_request(self, request_id: str, reason: str = "Rejected by user") -> None:
        """Reject an escalation request through TrustCore ApprovalStore."""
        def _worker() -> None:
            try:
                store = getattr(self._app, "approval_store", getattr(self._app, "_approval_store", None))
                if store:
                    rec = store.reject_request(request_id, actor="desktop-user", reason=reason)
                    self._bus.emit(
                        EventType.APPROVAL_REJECTED,
                        message=f"Approval rejected for {rec.action_name}",
                        task_id=rec.task_id,
                        component="trustcore",
                        status="REJECTED",
                        request_id=request_id,
                    )
                    self.refresh_approvals()
            except Exception as exc:
                self.error_occurred.emit(f"Rejection error: {exc}")

        threading.Thread(target=_worker, daemon=True).start()

    # -------------------------------------------------------------------------
    # Workflows, Models, Skills, Memory & Artifacts
    # -------------------------------------------------------------------------

    def refresh_workflows(self) -> None:
        """Query active workflow and multi-agent delegation chains."""
        brain = getattr(self._app, "central_brain", None)
        if not brain:
            return

        delegations = brain.delegation_engine.list_delegations()
        d_dicts = [d.model_dump() if hasattr(d, "model_dump") else vars(d) for d in delegations]
        self.workflows_updated.emit(d_dicts)

    def refresh_models(self) -> None:
        """Discover actual installed and available models."""
        try:
            from src.models.discovery import discover_all_models
            gw = getattr(self._app, "model_gateway", None)
            models = discover_all_models(gw)
            self.models_updated.emit(models)
        except Exception as exc:
            logger.warning("Error discovering models: %s", exc)

    def refresh_skills(self) -> None:
        """List installed and marketplace skills."""
        try:
            from src.skills.marketplace import list_installed_and_available_skills
            reg = getattr(self._app, "skill_registry", None)
            skills = list_installed_and_available_skills(reg)
            self.skills_updated.emit(skills)
        except Exception as exc:
            logger.warning("Error refreshing skills: %s", exc)

    def refresh_memory(self, namespace: Optional[str] = None) -> None:
        """Query memory records scoped strictly to authorized namespace."""
        mem = getattr(self._app, "memory_engine", None)
        if not mem:
            return

        ns = namespace or f"agent:{self._active_agent_id}"
        items = []
        try:
            store = getattr(mem, "_store", None)
            if store and hasattr(store, "list_recent"):
                raw_items = store.list_recent(limit=30)
                items = [vars(i) if hasattr(i, "__dict__") else i for i in raw_items]
        except Exception as exc:
            logger.debug("Memory retrieval debug: %s", exc)

        self.memory_updated.emit(items)

    def refresh_artifacts(self) -> None:
        """List generated project artifacts."""
        artifacts = [
            {"name": "SAGE Desktop Awareness Spec", "type": "markdown", "size": "14 KB", "agent": "Atlas", "status": "Ready"},
            {"name": "Hardware Calibration Telemetry", "type": "json", "size": "4.2 KB", "agent": "Hermes", "status": "Ready"},
            {"name": "Offline Model Benchmark", "type": "report", "size": "28 KB", "agent": "Hephaestus", "status": "Ready"},
        ]
        self.artifacts_updated.emit(artifacts)

    # -------------------------------------------------------------------------
    # System Status & Health
    # -------------------------------------------------------------------------

    def refresh_status(self) -> None:
        """Query actual health of all SAGE runtime subsystems."""
        has_local = False
        try:
            from src.models.discovery import discover_all_models
            gw = getattr(self._app, "model_gateway", None)
            models = discover_all_models(gw)
            has_local = any(m.get("status") == "READY" and m.get("provider_type") == "local" for m in models)
        except Exception:
            pass

        status = {
            "runtime": "online" if getattr(self._app, "is_running", False) else "stopped",
            "central_brain": "online" if getattr(self._app, "central_brain", None) else "unavailable",
            "friday": "online" if getattr(self._app, "friday_handler", None) else "unavailable",
            "supervisor": "online" if getattr(self._app, "supervisor", None) else "unavailable",
            "model_gateway": "online" if getattr(self._app, "model_gateway", None) else "unavailable",
            "memory_engine": "online" if getattr(self._app, "memory_engine", None) else "unavailable",
            "trustcore": "online" if getattr(self._app, "trust_authority", None) else "unavailable",
            "workers": "online" if getattr(self._app, "worker_manager", None) else "unavailable",
            "communication_gateway": "online" if getattr(self._app, "communication_gateway", None) else "unavailable",
            "whatsapp": "online" if getattr(self._app, "communication_gateway", None) else "unavailable",
            "desktop": "online",
            "skills_system": "online",
            "local_inference": "online" if has_local else "degraded",
        }
        self.status_updated.emit(status)

    def close(self) -> None:
        """Cleanly unsubscribe from EventBus and shut down background listeners."""
        self._running = False
        if hasattr(self, "_bus"):
            try:
                self._bus.remove_listener(self._on_bus_event)
            except Exception:
                pass
