"""tests/test_central_brain_and_multi_agent.py — Tests for Hybrid Central Brain, Multi-Agent System & Kiro UI.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & Independent Multi-Agent System
All tests run 100% offline without paid APIs or physical display requirements.
"""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch
import pytest

# Ensure headless Qt
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

_qt_app: QApplication | None = None


def _get_app() -> QApplication:
    global _qt_app
    if _qt_app is None:
        _qt_app = QApplication.instance() or QApplication(sys.argv)
    return _qt_app


@pytest.fixture(scope="session", autouse=True)
def qt_app():
    return _get_app()

from src.brain.central_brain import CentralBrain
from src.brain.delegation import DelegationEngine, DelegationSecurityError
from src.brain.models import (
    AgentProfile,
    AgentSession,
    AgentStatus,
    BrainIntent,
    DelegationRecord,
    DelegationStatus,
    IntentClassification,
)
from src.brain.registry import AgentRegistry, get_default_system_agents
from src.brain.whatsapp_router import WhatsAppContactRouter


# ===========================================================================
# 1. Central Brain & Intent Classification Tests
# ===========================================================================


class TestCentralBrainIntentClassification:
    @pytest.fixture
    def brain(self):
        reg = AgentRegistry()
        gw = MagicMock()
        return CentralBrain(agent_registry=reg, model_gateway=gw)

    def test_classify_status_query(self, brain):
        res = brain.classify_intent("What are you doing right now?")
        assert res.intent == BrainIntent.STATUS_QUERY
        assert res.target_agent_id == "friday"

    def test_classify_task_cancellation(self, brain):
        res = brain.classify_intent("Cancel task immediately")
        assert res.intent == BrainIntent.TASK_CANCEL
        assert res.target_agent_id == "friday"

    def test_classify_code_engineering_task(self, brain):
        res = brain.classify_intent("Fix failing test in tests/test_storage.py")
        assert res.intent == BrainIntent.ACTIONABLE_TASK
        assert res.target_agent_id == "coder"

    def test_classify_deep_research_task(self, brain):
        res = brain.classify_intent("Deep research into transformer memory bottlenecks")
        assert res.intent == BrainIntent.ACTIONABLE_TASK
        assert res.target_agent_id == "researcher"
        assert res.requires_reasoning_model is True

    def test_classify_desktop_automation_task(self, brain):
        res = brain.classify_intent("Inspect desktop window and click notepad")
        assert res.intent == BrainIntent.ACTIONABLE_TASK
        assert res.target_agent_id == "operator"

    def test_classify_multi_agent_delegation(self, brain):
        res = brain.classify_intent("Research and code a new plugin architecture")
        assert res.intent == BrainIntent.MULTI_AGENT_DELEGATION
        assert res.requires_reasoning_model is True

    def test_classify_routine_conversation(self, brain):
        res = brain.classify_intent("Good morning, how are you?")
        assert res.intent == BrainIntent.CONVERSATIONAL
        assert res.target_agent_id == "friday"


# ===========================================================================
# 2. Delegation Engine & Safety Boundaries Tests
# ===========================================================================


class TestDelegationEngine:
    def test_successful_delegation_creation(self):
        engine = DelegationEngine()
        rec = engine.create_delegation(
            source_agent_id="friday",
            destination_agent_id="researcher",
            assignment="Analyze codebase structure",
        )
        assert rec.delegation_id is not None
        assert rec.status == DelegationStatus.PENDING
        assert rec.source_agent_id == "friday"
        assert rec.destination_agent_id == "researcher"

    def test_self_delegation_prevented(self):
        engine = DelegationEngine()
        with pytest.raises(DelegationSecurityError, match="cannot delegate directly to itself"):
            engine.create_delegation(
                source_agent_id="coder",
                destination_agent_id="coder",
                assignment="Recursive self assignment",
            )

    def test_circular_delegation_prevented(self):
        engine = DelegationEngine()
        # Friday -> Researcher
        d1 = engine.create_delegation("friday", "researcher", "task 1")
        # Researcher -> Coder with same correlation chain
        d2 = engine.create_delegation(
            "researcher", "coder", "task 2", correlation_id=d1.correlation_id
        )
        # Coder -> Friday (Cycle detected!)
        with pytest.raises(DelegationSecurityError, match="Circular delegation detected"):
            engine.create_delegation(
                "coder", "friday", "cycle task", correlation_id=d1.correlation_id
            )

    def test_max_delegation_depth_enforced(self):
        engine = DelegationEngine(max_depth=2)
        d1 = engine.create_delegation("friday", "researcher", "step 1")
        # Chain depth is 2 (friday, researcher) -> next delegation exceeds limit
        with pytest.raises(DelegationSecurityError, match="depth limit"):
            engine.create_delegation(
                "researcher", "coder", "step 2", correlation_id=d1.correlation_id
            )

    def test_aggregate_results(self):
        reg = AgentRegistry()
        brain = CentralBrain(agent_registry=reg, model_gateway=MagicMock())
        d1 = brain.delegate("friday", "researcher", "Gather metrics")
        d2 = brain.delegate("friday", "coder", "Verify tests")
        agg = brain.aggregate_results([d1, d2])
        assert "Atlas" in agg
        assert "Hephaestus" in agg
        assert "COMPLETED" in agg


# ===========================================================================
# 3. Agent Registry & Strict Context Isolation Tests
# ===========================================================================


class TestAgentRegistryAndIsolation:
    def test_default_system_agents_seeded(self):
        reg = AgentRegistry()
        agents = reg.list_agents()
        agent_ids = {a.agent_id for a in agents}
        assert "friday" in agent_ids
        assert "researcher" in agent_ids
        assert "coder" in agent_ids
        assert "operator" in agent_ids

    def test_custom_agent_registration(self):
        reg = AgentRegistry()
        custom = AgentProfile(
            agent_id="sentinel",
            name="Sentinel",
            description="Security audit agent",
            persona="Strict compliance auditor",
            system_instructions="Audit all configurations.",
            model_alias="reasoning",
            memory_namespace="agent:sentinel",
        )
        reg.register_agent(custom)
        assert reg.get_agent("sentinel") is not None
        assert reg.get_agent("sentinel").name == "Sentinel"

    def test_independent_sessions_and_history_isolation(self):
        reg = AgentRegistry()
        s_friday = reg.create_session("friday", title="Friday Discussion")
        s_coder = reg.create_session("coder", title="Coder Implementation")

        reg.record_message(s_friday.session_id, role="user", content="Hello Friday")
        reg.record_message(s_friday.session_id, role="agent", content="Hello Human")

        reg.record_message(s_coder.session_id, role="user", content="Review PR 42")

        hist_friday = reg.get_session_history(s_friday.session_id)
        hist_coder = reg.get_session_history(s_coder.session_id)

        assert len(hist_friday) == 2
        assert len(hist_coder) == 1

        # Strict isolation: Friday's history contains zero messages from Coder's session
        assert all("Review PR 42" not in m.content for m in hist_friday)
        # Coder's history contains zero messages from Friday's session
        assert all("Hello Friday" not in m.content for m in hist_coder)

    def test_memory_namespace_scoping(self):
        reg = AgentRegistry()
        agents = reg.list_agents()
        namespaces = [a.memory_namespace for a in agents]
        # All namespaces are distinct
        assert len(namespaces) == len(set(namespaces))
        for ns in namespaces:
            assert ns.startswith("agent:")


# ===========================================================================
# 4. WhatsApp Contact Small-Model Routing & Escalation Tests
# ===========================================================================


class TestWhatsAppContactRouting:
    @pytest.fixture
    def router(self):
        gw = MagicMock()
        mock_resp = MagicMock()
        mock_resp.content = "Short local reply"
        gw.generate.return_value = mock_resp
        brain = MagicMock()
        brain.process_escalated_request.return_value = "Escalated brain synthesis"
        return WhatsAppContactRouter(model_gateway=gw, central_brain=brain)

    def test_routine_greeting_uses_small_model(self, router):
        is_routine, rationale = router.evaluate_routing("contact_123@s.whatsapp.net", "hello")
        assert is_routine is True
        reply = router.handle_contact_message("contact_123@s.whatsapp.net", "hello")
        assert reply == "Short local reply"
        assert router._gateway.generate.called

    def test_complex_operational_request_escalates(self, router):
        is_routine, rationale = router.evaluate_routing(
            "contact_456@s.whatsapp.net", "run pytest and fix failing tests in workspace"
        )
        assert is_routine is False
        reply = router.handle_contact_message(
            "contact_456@s.whatsapp.net", "run pytest and fix failing tests in workspace"
        )
        assert reply == "Escalated brain synthesis"
        assert router._brain.process_escalated_request.called

    def test_contact_history_isolation(self, router):
        router.handle_contact_message("user_a@s.whatsapp.net", "hi from a")
        router.handle_contact_message("user_b@s.whatsapp.net", "hi from b")

        sess_a = router.get_or_create_contact_session("user_a@s.whatsapp.net")
        sess_b = router.get_or_create_contact_session("user_b@s.whatsapp.net")

        assert sess_a.memory_namespace == "contact:user_a@s.whatsapp.net"
        assert sess_b.memory_namespace == "contact:user_b@s.whatsapp.net"

        # user_a history has no user_b messages
        assert all(turn["text"] != "hi from b" for turn in sess_a.history)
        # user_b history has no user_a messages
        assert all(turn["text"] != "hi from a" for turn in sess_b.history)


# ===========================================================================
# 5. Kiro Desktop UI Views Tests
# ===========================================================================


class TestKiroDesktopViews:
    @pytest.fixture
    def ui_service(self):
        from src.ui.desktop.service import UiService
        app = MagicMock()
        reg = AgentRegistry()
        app.agent_registry = reg
        app.central_brain = CentralBrain(agent_registry=reg, model_gateway=MagicMock())
        store = MagicMock()
        store.list_tasks.return_value = []
        store.list_task_steps.return_value = []
        app._state_store = store
        app.state_store = store
        app.approval_store = MagicMock()
        app.approval_store.list_pending.return_value = []
        return UiService(app)

    def test_topbar_widget_creation_and_search(self):
        from src.ui.desktop.topbar_widget import TopBarWidget
        top = TopBarWidget()
        searches: list[str] = []
        top.search_submitted.connect(searches.append)
        top.search_input.setText("find tests")
        top._on_search()
        assert searches == ["find tests"]

    def test_agents_view_renders_cards(self, ui_service):
        from src.ui.desktop.views.agents_view import AgentsView
        view = AgentsView(ui_service)
        agents = [
            {"agent_id": "a1", "name": "Agent 1", "description": "Desc 1", "model_alias": "fast"},
            {"agent_id": "a2", "name": "Agent 2", "description": "Desc 2", "model_alias": "coding"},
        ]
        view.set_agents(agents)
        assert len(view._all_agents) == 2

    def test_workflows_view_renders_delegation_cards(self, ui_service):
        from src.ui.desktop.views.workflows_view import WorkflowsView
        view = WorkflowsView(ui_service)
        wfs = [
            {"source_agent_id": "friday", "destination_agent_id": "researcher", "assignment": "Collect info", "status": "COMPLETED"}
        ]
        view.set_workflows(wfs)
        assert not view._empty_label.isVisible()

    def test_tasks_view_filtering(self, ui_service):
        from src.ui.desktop.views.tasks_view import TasksView
        view = TasksView(ui_service)
        tasks = [
            {"id": "t1", "title": "Inspect DB", "goal": "Check SQLite", "status": "COMPLETED"},
            {"id": "t2", "title": "Verify UI", "goal": "Check PySide6", "status": "RUNNING"},
        ]
        view.set_tasks(tasks)
        assert view.table.rowCount() == 2
        view._filter_tasks("Inspect")
        assert view.table.rowCount() == 1

    def test_status_view_component_matrix(self, ui_service):
        from src.ui.desktop.views.status_view import StatusView
        view = StatusView(ui_service)
        assert "central_brain" in view._cards
        assert "supervisor" in view._cards
        assert "model_gateway" in view._cards
        view.set_statuses({"central_brain": "online", "supervisor": "online"})
        assert "Online" in view._cards["central_brain"].status_pill.text()
