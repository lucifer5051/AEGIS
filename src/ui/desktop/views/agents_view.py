"""AgentsView — Kiro Crew inspired card grid for independent agent management.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.service import UiService
from src.ui.desktop.theme import (
    ACCENT_CYAN,
    ACCENT_GREEN,
    ACCENT_LAVENDER,
    ACCENT_PURPLE,
    BG_CARD,
    BG_HOVER,
    BG_INPUT,
    BG_MAIN,
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)


class CreateAgentDialog(QDialog):
    """Guided modal for creating and registering a new independent specialized agent."""

    agent_created = Signal(dict)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create Independent Agent")
        self.setFixedSize(480, 520)
        self.setStyleSheet(f"background-color: {BG_CARD}; color: {TEXT_PRIMARY};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        title = QLabel("Create New Agent Profile")
        title.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT_PRIMARY};")
        layout.addWidget(title)

        # Name & ID
        layout.addWidget(QLabel("Agent Name:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Sentinel, Hephaestus, Minerva")
        layout.addWidget(self.name_edit)

        # Description
        layout.addWidget(QLabel("Description:"))
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("Brief role summary")
        layout.addWidget(self.desc_edit)

        # Model Selection
        layout.addWidget(QLabel("Model Tier / Profile:"))
        self.model_combo = QComboBox()
        self.model_combo.addItems(["balanced", "reasoning", "coding", "fast", "local"])
        layout.addWidget(self.model_combo)

        # System Instructions
        layout.addWidget(QLabel("System Instructions:"))
        self.instructions_edit = QTextEdit()
        self.instructions_edit.setPlaceholderText("You are an independent specialist for...")
        self.instructions_edit.setFixedHeight(120)
        layout.addWidget(self.instructions_edit)

        # Action buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("Create Agent")
        save_btn.setStyleSheet(f"background-color: {ACCENT_PURPLE}; color: #ffffff; font-weight: 700;")
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def _on_save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            return
        agent_id = name.lower().replace(" ", "_")
        agent_data = {
            "agent_id": agent_id,
            "name": name,
            "description": self.desc_edit.text().strip() or f"Specialized {name} Agent",
            "persona": f"Specialized independent assistant {name}",
            "system_instructions": self.instructions_edit.toPlainText().strip() or f"You are {name}.",
            "model_alias": self.model_combo.currentText(),
            "skills": ["workspace.inspect"],
            "tool_permissions": ["file_read", "file_list"],
            "memory_namespace": f"agent:{agent_id}",
            "execution_mode": "BALANCED",
            "avatar_color": ACCENT_PURPLE,
        }
        self.agent_created.emit(agent_data)
        self.accept()


class AgentCard(QFrame):
    """Visual card displaying independent agent configuration and launch trigger."""

    chat_requested = Signal(str)  # agent_id

    def __init__(self, agent: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._agent_id = agent.get("agent_id", "")
        self.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 8px; padding: 14px;"
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Header: Avatar + Name + Status Pill
        hdr = QHBoxLayout()
        avatar = QLabel("🤖")
        avatar.setStyleSheet(
            f"font-size: 20px; background-color: #2F2147; "
            f"border: 1px solid {ACCENT_PURPLE}; border-radius: 14px; padding: 4px;"
        )
        hdr.addWidget(avatar)

        name_lbl = QLabel(agent.get("name", "Agent"))
        name_lbl.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {TEXT_PRIMARY};")
        hdr.addWidget(name_lbl)
        hdr.addStretch()

        status_lbl = QLabel("● Online")
        status_lbl.setStyleSheet(f"color: {ACCENT_GREEN}; font-size: 10px; font-weight: 600;")
        hdr.addWidget(status_lbl)
        layout.addLayout(hdr)

        # Description
        desc_lbl = QLabel(agent.get("description", ""))
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(desc_lbl)

        # Tags: Model & Memory Namespace
        tags_layout = QHBoxLayout()
        model_tag = QLabel(f"🎛 {agent.get('model_alias', 'balanced')}")
        model_tag.setStyleSheet(
            f"background-color: {BG_MAIN}; color: {ACCENT_LAVENDER}; "
            f"border: 1px solid {BORDER_SUBTLE}; border-radius: 4px; padding: 2px 6px; font-size: 10px;"
        )
        mem_tag = QLabel(f"🧠 {agent.get('memory_namespace', 'isolated')}")
        mem_tag.setStyleSheet(
            f"background-color: {BG_MAIN}; color: {ACCENT_CYAN}; "
            f"border: 1px solid {BORDER_SUBTLE}; border-radius: 4px; padding: 2px 6px; font-size: 10px;"
        )
        tags_layout.addWidget(model_tag)
        tags_layout.addWidget(mem_tag)
        tags_layout.addStretch()
        layout.addLayout(tags_layout)

        # Action: Chat with Agent
        chat_btn = QPushButton("💬 Open Conversation")
        chat_btn.setStyleSheet(
            f"background-color: #2F2147; border: 1px solid {ACCENT_PURPLE}; "
            f"border-radius: 6px; color: {TEXT_PRIMARY}; font-weight: 600; font-size: 11px; padding: 6px 0;"
        )
        chat_btn.clicked.connect(lambda: self.chat_requested.emit(self._agent_id))
        layout.addWidget(chat_btn)


class AgentsView(QWidget):
    """Grid view of independent agents with creation modal."""

    agent_selected_for_chat = Signal(str)  # agent_id

    def __init__(self, service: UiService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)

        # Top Bar: Heading + Search + Create Agent Button
        top_row = QHBoxLayout()
        title = QLabel("INDEPENDENT AGENTS")
        title.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {TEXT_PRIMARY}; letter-spacing: 1px;")
        top_row.addWidget(title)
        top_row.addStretch()

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter agents…")
        self.search_box.setFixedWidth(200)
        self.search_box.textChanged.connect(self._filter_agents)
        top_row.addWidget(self.search_box)

        new_agent_btn = QPushButton("+ Create Agent")
        new_agent_btn.setStyleSheet(
            f"background-color: {ACCENT_PURPLE}; border: none; "
            f"border-radius: 6px; color: #ffffff; font-weight: 700; padding: 6px 14px;"
        )
        new_agent_btn.clicked.connect(self._show_create_dialog)
        top_row.addWidget(new_agent_btn)
        layout.addLayout(top_row)

        # Scrollable Cards Grid
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(12)
        scroll.setWidget(self._container)
        layout.addWidget(scroll)

        self._all_agents: List[Dict[str, Any]] = []

        # Connect signals
        self._service.agents_updated.connect(self.set_agents)

    def _show_create_dialog(self) -> None:
        dlg = CreateAgentDialog(self)
        dlg.agent_created.connect(self._service.create_agent)
        dlg.exec()

    def set_agents(self, agents: List[Dict[str, Any]]) -> None:
        self._all_agents = agents
        self._render_cards(agents)

    def _render_cards(self, agents: List[Dict[str, Any]]) -> None:
        # Clear existing cards
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        cols = 3
        for idx, agent in enumerate(agents):
            card = AgentCard(agent)
            card.chat_requested.connect(self._on_agent_chat)
            row = idx // cols
            col = idx % cols
            self._grid.addWidget(card, row, col)

    def _filter_agents(self, query: str) -> None:
        q = query.strip().lower()
        if not q:
            self._render_cards(self._all_agents)
            return
        filtered = [
            a for a in self._all_agents
            if q in a.get("name", "").lower() or q in a.get("description", "").lower()
        ]
        self._render_cards(filtered)

    def _on_agent_chat(self, agent_id: str) -> None:
        self._service.select_agent(agent_id)
        self.agent_selected_for_chat.emit(agent_id)
