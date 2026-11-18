"""ConversationView — The primary multi-agent interaction workspace.

Combines session selector, conversational messages, inline task execution cards,
and collapsible technical inspector.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.activity_widget import LiveActivityWidget
from src.ui.desktop.approval_widget import ApprovalWidget
from src.ui.desktop.chat_widget import ChatInputEdit, EmptyConversationWidget, MessageItemWidget
from src.ui.desktop.plan_widget import ExecutionPlanWidget
from src.ui.desktop.service import UiService
from src.voice.service import get_voice_service
from src.ui.desktop.theme import (
    ACCENT_AMBER,
    ACCENT_CYAN,
    ACCENT_GREEN,
    ACCENT_LAVENDER,
    ACCENT_PURPLE,
    ACCENT_RED,
    BG_CARD,
    BG_HOVER,
    BG_INPUT,
    BG_MAIN,
    BG_SIDEBAR,
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)

logger = logging.getLogger(__name__)


class InlineExecutionCard(QFrame):
    """Visual inline task execution card embedded inside conversational stream."""

    def __init__(self, task_title: str, task_id: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-left: 4px solid {ACCENT_PURPLE}; border-radius: 8px; margin: 6px 12px;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        icon_lbl = QLabel("⚙ TASK IN PROGRESS")
        icon_lbl.setStyleSheet(f"color: {ACCENT_LAVENDER}; font-weight: 700; font-size: 11px;")
        id_lbl = QLabel(f"ID: {task_id[:12]}…")
        id_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px;")

        header.addWidget(icon_lbl)
        header.addStretch()
        header.addWidget(id_lbl)
        layout.addLayout(header)

        title_lbl = QLabel(f"<b>{task_title}</b>")
        title_lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 13px;")
        layout.addWidget(title_lbl)

        self.steps_container = QVBoxLayout()
        self.steps_container.setSpacing(4)
        layout.addLayout(self.steps_container)

    def set_steps(self, steps: List[Dict[str, Any]]) -> None:
        # Clear existing
        while self.steps_container.count():
            item = self.steps_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for s in steps:
            name = s.get("name", s.get("step_name", "Step"))
            status = str(s.get("status", "PENDING")).upper()
            if status in ("SUCCESS", "COMPLETED"):
                icon, color = "✔", ACCENT_GREEN
            elif status in ("RUNNING", "IN_PROGRESS"):
                icon, color = "●", ACCENT_AMBER
            elif status in ("FAILED", "ERROR"):
                icon, color = "✘", ACCENT_RED
            else:
                icon, color = "○", TEXT_DIM

            row = QHBoxLayout()
            icon_lbl = QLabel(icon)
            icon_lbl.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 11px;")
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
            status_lbl = QLabel(status.lower())
            status_lbl.setStyleSheet(f"color: {color}; font-size: 10px; font-style: italic;")

            row.addWidget(icon_lbl)
            row.addWidget(name_lbl)
            row.addStretch()
            row.addWidget(status_lbl)

            step_widget = QWidget()
            step_widget.setLayout(row)
            self.steps_container.addWidget(step_widget)


class ConversationView(QWidget):
    """Main conversational view supporting independent agent sessions and technical inspection."""

    def __init__(self, service: UiService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service
        self._voice = get_voice_service()
        self._active_agent: Optional[Dict[str, Any]] = None
        self._all_sessions: List[Dict[str, Any]] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 3-Pane Horizontal Splitter: Sessions (left) | Conversation (center) | Inspector (right)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        # 1. Sessions List Panel (Left)
        self._sessions_panel = self._build_sessions_panel()
        self.splitter.addWidget(self._sessions_panel)

        # 2. Main Conversation Panel (Center)
        self._center_panel = self._build_center_panel()
        self.splitter.addWidget(self._center_panel)

        # 3. Technical Inspector Panel (Right)
        self._inspector_panel = self._build_inspector_panel()
        self.splitter.addWidget(self._inspector_panel)

        self.splitter.setSizes([200, 580, 280])
        self.splitter.setStretchFactor(1, 1)  # Center stretches
        layout.addWidget(self.splitter)

        # Connect signals
        self._connect_signals()

    # -------------------------------------------------------------------------
    # Panel Builders
    # -------------------------------------------------------------------------

    def _build_sessions_panel(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background-color: {BG_SIDEBAR}; border-right: 1px solid {BORDER_SUBTLE};"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 10, 8, 10)
        layout.setSpacing(8)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("SESSIONS")
        title.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px; font-weight: 700; letter-spacing: 1px;")
        new_btn = QPushButton("+ New")
        new_btn.setFixedSize(50, 24)
        new_btn.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_PRIMARY}; font-size: 11px; font-weight: 600;"
        )
        new_btn.clicked.connect(lambda: self._service.create_session())
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(new_btn)
        layout.addLayout(hdr)

        # Search box
        self.session_search = QLineEdit()
        self.session_search.setPlaceholderText("Search sessions…")
        self.session_search.setStyleSheet(
            f"background-color: {BG_INPUT}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_PRIMARY}; padding: 3px 6px; font-size: 11px;"
        )
        self.session_search.textChanged.connect(self._filter_sessions)
        layout.addWidget(self.session_search)

        # Sessions list
        self.session_list = QListWidget()
        self.session_list.setStyleSheet(f"background-color: transparent; border: none;")
        self.session_list.itemClicked.connect(self._on_session_clicked)
        layout.addWidget(self.session_list)

        return frame

    def _build_center_panel(self) -> QWidget:
        center = QWidget()
        center.setStyleSheet(f"background-color: {BG_MAIN};")
        layout = QVBoxLayout(center)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Agent Banner
        self.agent_banner = QFrame()
        self.agent_banner.setFixedHeight(54)
        self.agent_banner.setStyleSheet(
            f"background-color: {BG_CARD}; border-bottom: 1px solid {BORDER_SUBTLE};"
        )
        b_layout = QHBoxLayout(self.agent_banner)
        b_layout.setContentsMargins(16, 8, 16, 8)

        self.agent_avatar = QLabel("🤖")
        self.agent_avatar.setStyleSheet(
            f"font-size: 18px; background-color: #2F2147; "
            f"border: 1px solid {ACCENT_PURPLE}; border-radius: 16px; padding: 4px;"
        )
        self.agent_name_label = QLabel("Friday")
        self.agent_name_label.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {TEXT_PRIMARY};")
        self.agent_desc_label = QLabel("Executive Conversational Companion")
        self.agent_desc_label.setStyleSheet(f"font-size: 11px; color: {TEXT_MUTED};")

        v_box = QVBoxLayout()
        v_box.setSpacing(1)
        v_box.addWidget(self.agent_name_label)
        v_box.addWidget(self.agent_desc_label)

        b_layout.addWidget(self.agent_avatar)
        b_layout.addSpacing(8)
        b_layout.addLayout(v_box)
        b_layout.addStretch()

        self.agent_model_badge = QLabel("balanced")
        self.agent_model_badge.setStyleSheet(
            f"background-color: {BG_MAIN}; color: {ACCENT_LAVENDER}; "
            f"border: 1px solid {BORDER_SUBTLE}; border-radius: 10px; padding: 2px 8px; font-size: 10px;"
        )
        b_layout.addWidget(self.agent_model_badge)

        self.typing_label = QLabel("")
        self.typing_label.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 11px; font-style: italic;")
        b_layout.addWidget(self.typing_label)

        layout.addWidget(self.agent_banner)

        # Message stream
        self.msg_list = QListWidget()
        self.msg_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.msg_list.setStyleSheet(f"background-color: {BG_MAIN}; border: none; padding: 12px;")
        layout.addWidget(self.msg_list, stretch=1)

        # Composer Area
        composer_container = QFrame()
        composer_container.setStyleSheet(
            f"background-color: {BG_CARD}; border-top: 1px solid {BORDER_SUBTLE};"
        )
        c_layout = QVBoxLayout(composer_container)
        c_layout.setContentsMargins(14, 10, 14, 10)
        c_layout.setSpacing(6)

        # Model Selector Bar
        model_bar = QHBoxLayout()
        model_bar.setSpacing(8)
        m_lbl = QLabel("Model:")
        m_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; font-weight: 600;")
        self.model_selector = QComboBox()
        self.model_selector.setStyleSheet(
            f"background-color: {BG_INPUT}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_PRIMARY}; padding: 2px 8px; font-size: 11px;"
        )
        self.model_selector.addItem("⚡ Auto (Central Brain Guided)", "Auto")
        self.model_selector.addItem("🤖 Agent Preferred", "balanced")
        self.model_selector.addItem("🧠 Deep Reasoning", "reasoning")
        self.model_selector.addItem("💻 Code Specialist", "coding")
        self.model_selector.addItem("🏠 Resident Fallback (sage-local-v1)", "sage-local-v1")
        self.model_selector.addItem("📦 Local GGUF (qwen35-4b-q4km)", "qwen35-4b-q4km")
        self.model_selector.addItem("☁ OpenAI GPT-4o (Cloud)", "cloud-openai-default")
        self.model_selector.addItem("☁ Anthropic Claude 3.5 Sonnet (Cloud)", "cloud-anthropic-default")
        self.model_selector.addItem("☁ Google Gemini 1.5 Pro (Cloud)", "cloud-gemini-default")

        model_bar.addWidget(m_lbl)
        model_bar.addWidget(self.model_selector)
        model_bar.addStretch()

        self.auto_speak_cb = QCheckBox("🔊 Voice Readout")
        self.auto_speak_cb.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        model_bar.addWidget(self.auto_speak_cb)

        self.toggle_inspector_btn = QPushButton("▶ Hide Inspector")
        self.toggle_inspector_btn.setFixedHeight(22)
        self.toggle_inspector_btn.setStyleSheet(
            f"background-color: {BG_MAIN}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_MUTED}; padding: 2px 8px; font-size: 10px;"
        )
        self.toggle_inspector_btn.clicked.connect(self._toggle_inspector)
        model_bar.addWidget(self.toggle_inspector_btn)

        c_layout.addLayout(model_bar)

        # Multiline text input
        self.input_edit = ChatInputEdit()
        self.input_edit.setPlaceholderText("Message agent… (Enter to send, Shift+Enter for newline)")
        self.input_edit.setFixedHeight(50)
        self.input_edit.setStyleSheet(
            f"background-color: {BG_INPUT}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 6px; color: {TEXT_PRIMARY}; padding: 6px 10px;"
        )
        self.input_edit.send_requested.connect(self._on_send)
        c_layout.addWidget(self.input_edit)

        # Toolbar beneath input
        tb_layout = QHBoxLayout()
        self.active_agent_badge = QLabel("🤖 Talking with Friday")
        self.active_agent_badge.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        tb_layout.addWidget(self.active_agent_badge)
        tb_layout.addStretch()

        self.ptt_btn = QPushButton("🎤 Push-to-Talk")
        self.ptt_btn.setStyleSheet(
            f"background-color: {BG_MAIN}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_PRIMARY}; padding: 3px 10px; font-size: 11px; font-weight: 600;"
        )
        self.ptt_btn.clicked.connect(self._on_ptt_toggle)
        tb_layout.addWidget(self.ptt_btn)

        self.attach_btn = QPushButton("📎 Attach")
        self.attach_btn.setStyleSheet(
            f"background-color: {BG_MAIN}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_MUTED}; padding: 3px 10px; font-size: 11px;"
        )
        tb_layout.addWidget(self.attach_btn)

        self.cancel_task_btn = QPushButton("⏹ Cancel Task")
        self.cancel_task_btn.setStyleSheet(
            f"background-color: {BG_MAIN}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {ACCENT_AMBER}; padding: 3px 10px; font-size: 11px;"
        )
        self.cancel_task_btn.clicked.connect(self._on_cancel_task)
        tb_layout.addWidget(self.cancel_task_btn)

        self.send_btn = QPushButton("Send")
        self.send_btn.setFixedSize(65, 26)
        self.send_btn.setStyleSheet(
            f"background-color: {ACCENT_PURPLE}; border: none; "
            f"border-radius: 4px; color: #ffffff; font-weight: 700; font-size: 11px;"
        )
        self.send_btn.clicked.connect(self._on_send)
        tb_layout.addWidget(self.send_btn)

        c_layout.addLayout(tb_layout)
        layout.addWidget(composer_container)

        return center

    def _build_inspector_panel(self) -> QFrame:
        inspector = QFrame()
        inspector.setStyleSheet(
            f"background-color: {BG_SIDEBAR}; border-left: 1px solid {BORDER_SUBTLE};"
        )
        v_layout = QVBoxLayout(inspector)
        v_layout.setContentsMargins(10, 10, 10, 10)
        v_layout.setSpacing(10)

        # Title
        hdr = QLabel("TECHNICAL INSPECTOR")
        hdr.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px; font-weight: 700; letter-spacing: 1px;")
        v_layout.addWidget(hdr)

        # Sub-widgets
        self.plan_widget = ExecutionPlanWidget()
        v_layout.addWidget(self.plan_widget, stretch=2)

        self.activity_widget = LiveActivityWidget()
        v_layout.addWidget(self.activity_widget, stretch=2)

        self.approval_widget = ApprovalWidget()
        v_layout.addWidget(self.approval_widget, stretch=1)

        return inspector

    # -------------------------------------------------------------------------
    # Signals & Slots
    # -------------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._service.message_received.connect(self.add_message)
        self._service.session_messages_loaded.connect(self.on_session_messages_loaded)
        self._service.typing_state_changed.connect(self.set_typing)
        self._service.sessions_updated.connect(self.set_sessions)
        self._service.active_agent_changed.connect(self.set_active_agent)
        self._service.plan_updated.connect(self.plan_widget.set_steps)
        self._service.activity_event.connect(self.activity_widget.add_event)
        self._service.approvals_updated.connect(self.approval_widget.set_approvals)
        self.approval_widget.approve_requested.connect(self._service.approve_request)
        self.approval_widget.reject_requested.connect(self._service.reject_request)

        # Voice signals
        self._voice.transcription_ready.connect(self._on_transcription_ready)
        self._voice.recording_started.connect(self._on_recording_started)
        self._voice.recording_stopped.connect(self._on_recording_stopped)

    def on_session_messages_loaded(self, messages: List[Dict[str, Any]]) -> None:
        self.msg_list.clear()
        if not messages:
            agent_name = (self._active_agent or {}).get("name", "Friday")
            agent_desc = (self._active_agent or {}).get("description", "Ready to assist you.")
            item = QListWidgetItem(self.msg_list)
            empty_widget = EmptyConversationWidget(agent_name=agent_name, description=agent_desc)
            item.setSizeHint(empty_widget.sizeHint())
            self.msg_list.addItem(item)
            self.msg_list.setItemWidget(item, empty_widget)
        else:
            for msg in messages:
                role = msg.get("role", "assistant")
                content = msg.get("content", "")
                ts = msg.get("timestamp", "")
                m_name = msg.get("model_name", "")
                agent_name = msg.get("agent_name") or (self._active_agent or {}).get("name")
                self.add_message(role, content, ts, model_name=m_name, agent_name=agent_name)

    def set_active_agent(self, agent_dict: Dict[str, Any]) -> None:
        self._active_agent = agent_dict
        name = agent_dict.get("name", "Agent")
        desc = agent_dict.get("description", "")
        model = agent_dict.get("model_alias", "balanced")
        agent_id = str(agent_dict.get("agent_id", "")).lower()

        avatar_map = {
            "friday": "🤖",
            "researcher": "🔬",
            "atlas": "🔬",
            "coder": "⚡",
            "hephaestus": "⚡",
            "operator": "🖥",
            "hermes": "🖥",
        }
        av = avatar_map.get(agent_id, "🤖")
        self.agent_avatar.setText(av)
        self.agent_name_label.setText(name)
        self.agent_desc_label.setText(desc[:45] + "…" if len(desc) > 45 else desc)
        self.agent_model_badge.setText(model)
        self.active_agent_badge.setText(f"{av} Talking with {name}")
        self.input_edit.setPlaceholderText(f"Message {name}… (Enter to send, Shift+Enter for newline)")

        # If current conversation has only an EmptyConversationWidget, refresh it with the new agent
        if self.msg_list.count() == 1:
            first_item = self.msg_list.item(0)
            widget = self.msg_list.itemWidget(first_item)
            if isinstance(widget, EmptyConversationWidget):
                self.msg_list.clear()
                item = QListWidgetItem(self.msg_list)
                empty_widget = EmptyConversationWidget(agent_name=name, description=desc)
                item.setSizeHint(empty_widget.sizeHint())
                self.msg_list.addItem(item)
                self.msg_list.setItemWidget(item, empty_widget)

    def set_sessions(self, sessions: List[Dict[str, Any]]) -> None:
        self._all_sessions = sessions
        self._render_filtered_sessions(sessions)

    def _filter_sessions(self, query: str) -> None:
        q = query.strip().lower()
        if not q:
            self._render_filtered_sessions(self._all_sessions)
            return
        filtered = [s for s in self._all_sessions if q in s.get("title", "").lower() or q in s.get("preview", "").lower()]
        self._render_filtered_sessions(filtered)

    def _render_filtered_sessions(self, sessions: List[Dict[str, Any]]) -> None:
        self.session_list.clear()
        for s in sessions:
            title = s.get("title", "Conversation")
            time_str = s.get("last_activity", "")[:10]
            item = QListWidgetItem(f"💬 {title}\n    {time_str}")
            item.setData(Qt.ItemDataRole.UserRole, s.get("session_id"))
            self.session_list.addItem(item)

    def _on_session_clicked(self, item: QListWidgetItem) -> None:
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if session_id:
            self._service.select_session(session_id)

    def _on_send(self) -> None:
        text = self.input_edit.toPlainText().strip()
        if text:
            selected_model = self.model_selector.currentData() if hasattr(self, "model_selector") else None
            self._service.send_chat_message(text, model_override=selected_model)
            self.input_edit.clear()

    def _on_cancel_task(self) -> None:
        if self._service._active_task_id:
            self._service.cancel_task(self._service._active_task_id)

    def _on_ptt_toggle(self) -> None:
        if getattr(self._voice, "_is_recording", False):
            self._voice.stop_recording()
        else:
            self._voice.start_recording()

    def _on_recording_started(self) -> None:
        self.ptt_btn.setText("🔴 Stop Recording")
        self.ptt_btn.setStyleSheet(
            f"background-color: {ACCENT_RED}; border: none; "
            f"border-radius: 4px; color: #ffffff; padding: 3px 10px; font-size: 11px; font-weight: 700;"
        )

    def _on_recording_stopped(self, wav_path: str) -> None:
        self.ptt_btn.setText("🎤 Push-to-Talk")
        self.ptt_btn.setStyleSheet(
            f"background-color: {BG_MAIN}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_PRIMARY}; padding: 3px 10px; font-size: 11px; font-weight: 600;"
        )

    def _on_transcription_ready(self, text: str) -> None:
        existing = self.input_edit.toPlainText().strip()
        new_text = f"{existing} {text}".strip() if existing else text
        self.input_edit.setPlainText(new_text)
        self.input_edit.setFocus()

    def _toggle_inspector(self) -> None:
        is_visible = self._inspector_panel.isVisible()
        self._inspector_panel.setVisible(not is_visible)
        self.toggle_inspector_btn.setText("▶ Show Inspector" if is_visible else "▶ Hide Inspector")

    def add_message(
        self,
        role: str,
        text: str,
        timestamp: str = "",
        model_name: str = "",
        agent_name: Optional[str] = None,
    ) -> None:
        if self.msg_list.count() == 1:
            first_item = self.msg_list.item(0)
            widget = self.msg_list.itemWidget(first_item)
            if isinstance(widget, EmptyConversationWidget):
                self.msg_list.takeItem(0)

        item = QListWidgetItem(self.msg_list)
        resolved_agent = agent_name or (self._active_agent or {}).get("name")
        widget = MessageItemWidget(
            role,
            text,
            timestamp,
            agent_name=resolved_agent,
            model_name=model_name,
        )
        item.setSizeHint(widget.sizeHint())
        self.msg_list.addItem(item)
        self.msg_list.setItemWidget(item, widget)
        self.msg_list.scrollToBottom()

        # Optional Voice Readout
        if hasattr(self, "auto_speak_cb") and self.auto_speak_cb.isChecked() and role.lower() != "user":
            self._voice.speak(text)

    def set_typing(self, is_typing: bool) -> None:
        if is_typing:
            self.typing_label.setText("Agent thinking…")
        else:
            self.typing_label.setText("")
