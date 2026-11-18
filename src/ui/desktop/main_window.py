"""SageMainWindow — The primary desktop application window for SAGE.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.service import UiService
from src.ui.desktop.sidebar_widget import SidebarWidget
from src.ui.desktop.theme import DARK_STYLESHEET
from src.ui.desktop.topbar_widget import TopBarWidget
from src.ui.desktop.views.agents_view import AgentsView
from src.ui.desktop.views.artifacts_view import ArtifactsView
from src.ui.desktop.views.config_view import ConfigView
from src.ui.desktop.views.conversation_view import ConversationView
from src.ui.desktop.views.memory_view import MemoryView
from src.ui.desktop.views.models_view import ModelsView
from src.ui.desktop.views.settings_view import SettingsView
from src.ui.desktop.views.skills_view import SkillsView
from src.ui.desktop.views.status_view import StatusView
from src.ui.desktop.views.tasks_view import TasksView
from src.ui.desktop.views.workflows_view import WorkflowsView

if TYPE_CHECKING:
    from src.runtime import SageApplication

logger = logging.getLogger(__name__)

_REFRESH_INTERVAL_MS = 5_000
_WELCOME = (
    "Hello! I am Friday, your primary conversational partner and executive advisor. "
    "I can coordinate with specialized agents (Atlas for Research, Hephaestus for Code, Hermes for Desktop Automation) "
    "to help you plan, investigate, build, and verify complex goals. How can I assist you today?"
)


class SageMainWindow(QMainWindow):
    """Kiro Crew-inspired PySide6 desktop workstation for SAGE multi-agent operations."""

    def __init__(self, sage_app: "SageApplication") -> None:
        super().__init__()
        self._sage_app = sage_app

        # 1. Core service bridge
        self._service = UiService(sage_app, parent=self)

        # 2. Window Chrome & Stylesheet
        self.setWindowTitle("SAGE — System for Autonomous Guidance & Execution")
        self.setMinimumSize(1180, 740)
        self.setStyleSheet(DARK_STYLESHEET)

        # 3. Main Layout Structure
        root_widget = QWidget()
        root_layout = QVBoxLayout(root_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Top Bar
        self.top_bar = TopBarWidget()
        root_layout.addWidget(self.top_bar)

        # Body (Sidebar + Stacked Workspace)
        body_layout = QHBoxLayout()
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self.sidebar = SidebarWidget()
        body_layout.addWidget(self.sidebar)

        # Views Stack
        self.stack = QStackedWidget()
        self.view_conversation = ConversationView(self._service)  # 0: sessions / conversation
        self.view_agents = AgentsView(self._service)              # 1: agents
        self.view_skills = SkillsView(self._service)              # 2: skills
        self.view_models = ModelsView(self._service)              # 3: models
        self.view_workflows = WorkflowsView(self._service)        # 4: workflows
        self.view_tasks = TasksView(self._service)                # 5: tasks
        self.view_artifacts = ArtifactsView(self._service)        # 6: artifacts
        self.view_memory = MemoryView(self._service)              # 7: memory
        self.view_settings = SettingsView(self._service)          # 8: settings
        self.view_status = StatusView(self._service)              # 9: status

        self.stack.addWidget(self.view_conversation)  # 0
        self.stack.addWidget(self.view_agents)        # 1
        self.stack.addWidget(self.view_skills)        # 2
        self.stack.addWidget(self.view_models)        # 3
        self.stack.addWidget(self.view_workflows)     # 4
        self.stack.addWidget(self.view_tasks)         # 5
        self.stack.addWidget(self.view_artifacts)     # 6
        self.stack.addWidget(self.view_memory)        # 7
        self.stack.addWidget(self.view_settings)      # 8
        self.stack.addWidget(self.view_status)        # 9

        body_layout.addWidget(self.stack, stretch=1)
        root_layout.addLayout(body_layout, stretch=1)

        self.setCentralWidget(root_widget)

        # 4. Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("SAGE multi-agent runtime ready.")

        # 5. Connect Signal Handlers
        self._connect_signals()

        # 6. Polling Timer
        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

        # 7. Initial Data Load
        self._service.refresh_agents()
        self._service.refresh_sessions()
        self._service.refresh_tasks()
        self._service.refresh_status()
        self._service.refresh_approvals()

        # Select default agent and restore history
        self._service.select_agent("friday")

    def _connect_signals(self) -> None:
        self.sidebar.nav_changed.connect(self._switch_view)
        self.top_bar.settings_requested.connect(lambda: self._switch_view("settings"))
        self.top_bar.search_submitted.connect(self._handle_global_search)

        self._service.status_updated.connect(self._on_status_updated)
        self._service.active_agent_changed.connect(self._on_active_agent_changed)
        self._service.error_occurred.connect(
            lambda msg: self.status_bar.showMessage(f"⚠ {msg}", 8000)
        )

        # When agent clicked in AgentsView, jump to conversation view
        self.view_agents.agent_selected_for_chat.connect(self._on_agent_selected_for_chat)

        # When task selected in TasksView, jump to conversation view inspection
        self.view_tasks.task_selected.connect(lambda _: self._switch_view("sessions"))

    def _switch_view(self, view_id: str) -> None:
        view_map = {
            "sessions": 0,
            "conversation": 0,
            "agents": 1,
            "skills": 2,
            "models": 3,
            "workflows": 4,
            "tasks": 5,
            "artifacts": 6,
            "schedules": 5,
            "memory": 7,
            "settings": 8,
            "agent_config": 8,
            "model_config": 3,
            "integrations": 8,
            "status": 9,
        }
        idx = view_map.get(view_id, 0)
        self.stack.setCurrentIndex(idx)
        self.sidebar.select_view(view_id)

        # Trigger data refreshes on navigation
        if view_id in ("sessions", "conversation"):
            self._service.refresh_sessions()
        elif view_id == "agents":
            self._service.refresh_agents()
        elif view_id == "skills":
            self._service.refresh_skills()
        elif view_id in ("models", "model_config"):
            self._service.refresh_models()
        elif view_id == "workflows":
            self._service.refresh_workflows()
        elif view_id in ("tasks", "schedules"):
            self._service.refresh_tasks()
        elif view_id == "artifacts":
            self._service.refresh_artifacts()
        elif view_id == "memory":
            self._service.refresh_memory()
        elif view_id == "status":
            self._service.refresh_status()

    def _on_agent_selected_for_chat(self, agent_id: str) -> None:
        self._switch_view("sessions")

    def _on_active_agent_changed(self, agent_dict: Dict[str, Any]) -> None:
        name = agent_dict.get("name", "Friday")
        model = agent_dict.get("model_alias", "balanced")
        self.top_bar.set_active_agent(name, model)

    def _on_status_updated(self, status: Dict[str, Any]) -> None:
        is_online = status.get("runtime") == "online"
        self.top_bar.set_status(is_online, "Runtime Online" if is_online else "Runtime Stopped")
        sup = status.get("supervisor", "unknown")
        gw = status.get("model_gateway", "unknown")
        brain = status.get("central_brain", "unknown")
        self.status_bar.showMessage(
            f"SAGE Online  |  Brain: {brain}  |  Supervisor: {sup}  |  Model: {gw}"
        )

    def _handle_global_search(self, query: str) -> None:
        self.status_bar.showMessage(f"Searching: '{query}'…", 3000)
        # Search across tasks or agents
        self._switch_view("tasks")
        self.view_tasks.filter_box.setText(query)

    def _poll(self) -> None:
        self._service.refresh_status()
        self._service.refresh_approvals()

    @property
    def _chat(self) -> Any:
        return self.view_conversation

    def closeEvent(self, event: Any) -> None:
        self._timer.stop()
        try:
            self._service.close()
        except Exception:
            pass
        try:
            self._sage_app.stop()
        except Exception:
            pass
        event.accept()
