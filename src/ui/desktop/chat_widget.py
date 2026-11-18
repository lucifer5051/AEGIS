"""SAGE PySide6 Desktop — Conversation Widget & Message Bubbles.

Provides Markdown-capable, high-contrast message bubbles, auto-wrapping,
timestamps, and agent-specific visual identities.

Canonical Reference:
    PROMPT 080: Functional Stabilization, Conversation Repair & Model Manager
"""
from __future__ import annotations

import html
import re
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.theme import (
    ACCENT_AMBER,
    ACCENT_CYAN,
    ACCENT_GREEN,
    ACCENT_LAVENDER,
    ACCENT_PURPLE,
    ACCENT_RED,
    BG_CARD,
    BG_INPUT,
    BG_MAIN,
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)


class ChatInputEdit(QTextEdit):
    """Custom QTextEdit capturing Enter to send and Shift+Enter for newlines."""

    send_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Return and not (event.modifiers() & Qt.ShiftModifier):
            event.accept()
            self.send_requested.emit()
        else:
            super().keyPressEvent(event)


def _format_markdown_to_html(text: str) -> str:
    """Format markdown text including headers, bold, italics, inline code, and code blocks."""
    if not text:
        return ""

    escaped = html.escape(text)

    # 1. Multi-line code blocks: ```lang\ncode\n```
    def _code_block_sub(match: re.Match) -> str:
        code_content = match.group(1).strip()
        return (
            f"<div style='background-color: #121016; border: 1px solid #352F3D; "
            f"border-radius: 6px; padding: 10px; margin: 8px 0; font-family: Cascadia Code, Consolas, monospace; "
            f"font-size: 12px; color: #00D2FF; white-space: pre-wrap;'>{code_content}</div>"
        )
    escaped = re.sub(r"```(?:[a-zA-Z0-9_-]+)?\n(.*?)```", _code_block_sub, escaped, flags=re.DOTALL)

    # 2. Inline code: `code`
    escaped = re.sub(
        r"`([^`]+)`",
        r"<code style='background-color: #1A1622; color: #00D2FF; padding: 2px 5px; border-radius: 4px; font-family: Cascadia Code, monospace;'>\1</code>",
        escaped,
    )

    # 3. Bold: **text**
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)

    # 4. Italic: *text*
    escaped = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", escaped)

    # 5. Headers: ### Header
    escaped = re.sub(r"^### (.*?)$", r"<h4 style='color: #EDEDEF; margin: 6px 0 2px 0;'>\1</h4>", escaped, flags=re.MULTILINE)
    escaped = re.sub(r"^## (.*?)$", r"<h3 style='color: #EDEDEF; margin: 8px 0 4px 0;'>\1</h3>", escaped, flags=re.MULTILINE)
    escaped = re.sub(r"^# (.*?)$", r"<h2 style='color: #EDEDEF; margin: 10px 0 6px 0;'>\1</h2>", escaped, flags=re.MULTILINE)

    # 6. Bullet lists
    escaped = re.sub(r"^\s*[-*]\s+(.*?)$", r"• \1<br/>", escaped, flags=re.MULTILINE)

    # 7. Newlines to <br/>
    escaped = escaped.replace("\n", "<br/>")

    return escaped


class MessageItemWidget(QWidget):
    """High-contrast styled widget representing a single user or agent message bubble."""

    def __init__(
        self,
        role: str,
        text: str,
        timestamp: str = "",
        agent_name: Optional[str] = None,
        model_name: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 6, 12, 6)
        root.setSpacing(12)

        role_lower = role.lower().strip()
        is_user = role_lower == "user"

        # Determine agent styling
        if is_user:
            avatar_char = "👤"
            display_name = "You"
            border_color = "#3B4252"
            bubble_bg = "#1B2232"
            avatar_bg = "#2E3440"
            role_color = "#88C0D0"
        elif role_lower in ("friday", "assistant"):
            avatar_char = "🤖"
            display_name = agent_name or "Friday"
            border_color = ACCENT_PURPLE
            bubble_bg = "#241D32"
            avatar_bg = "#312646"
            role_color = ACCENT_LAVENDER
        elif role_lower in ("researcher", "atlas"):
            avatar_char = "🔬"
            display_name = agent_name or "Atlas"
            border_color = ACCENT_CYAN
            bubble_bg = "#162832"
            avatar_bg = "#1B3B4B"
            role_color = ACCENT_CYAN
        elif role_lower in ("coder", "hephaestus"):
            avatar_char = "⚡"
            display_name = agent_name or "Hephaestus"
            border_color = ACCENT_GREEN
            bubble_bg = "#162B21"
            avatar_bg = "#1A402E"
            role_color = ACCENT_GREEN
        elif role_lower in ("operator", "hermes"):
            avatar_char = "🖥"
            display_name = agent_name or "Hermes"
            border_color = ACCENT_AMBER
            bubble_bg = "#2E2418"
            avatar_bg = "#45321B"
            role_color = ACCENT_AMBER
        elif role_lower in ("system", "error"):
            avatar_char = "⚠"
            display_name = "System Alert"
            border_color = ACCENT_RED
            bubble_bg = "#2D1818"
            avatar_bg = "#451B1B"
            role_color = ACCENT_RED
        else:
            avatar_char = "🤖"
            display_name = agent_name or role.capitalize()
            border_color = ACCENT_PURPLE
            bubble_bg = BG_CARD
            avatar_bg = "#2F2147"
            role_color = ACCENT_LAVENDER

        # Avatar Label
        avatar_lbl = QLabel(avatar_char)
        avatar_lbl.setFixedSize(32, 32)
        avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar_lbl.setStyleSheet(
            f"background-color: {avatar_bg}; border: 1px solid {border_color}; "
            f"border-radius: 16px; font-size: 15px;"
        )

        # Bubble Container
        bubble = QFrame()
        bubble.setStyleSheet(
            f"background-color: {bubble_bg}; border: 1px solid {border_color}; "
            f"border-radius: 8px;"
        )
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(12, 10, 12, 10)
        bubble_layout.setSpacing(6)

        # Header: Name + Timestamp
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        name_lbl = QLabel(display_name)
        name_lbl.setStyleSheet(f"font-weight: 700; font-size: 12px; color: {role_color};")
        header_layout.addWidget(name_lbl)

        if model_name:
            model_lbl = QLabel(f"[{model_name}]")
            model_lbl.setStyleSheet(
                f"background-color: #121016; color: {ACCENT_LAVENDER}; "
                f"border: 1px solid {BORDER_SUBTLE}; border-radius: 3px; padding: 1px 5px; font-size: 9px; font-weight: 600;"
            )
            header_layout.addWidget(model_lbl)

        if timestamp:
            time_lbl = QLabel(timestamp)
            time_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px;")
            header_layout.addWidget(time_lbl)

        header_layout.addStretch()

        copy_btn = QPushButton("📋")
        copy_btn.setToolTip("Copy message")
        copy_btn.setFixedSize(22, 18)
        copy_btn.setStyleSheet(f"background: transparent; border: none; color: {TEXT_MUTED}; font-size: 11px;")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(text))
        header_layout.addWidget(copy_btn)

        bubble_layout.addLayout(header_layout)

        # Body: Rich HTML/Markdown text
        formatted_html = _format_markdown_to_html(text)
        content_lbl = QLabel()
        content_lbl.setTextFormat(Qt.TextFormat.RichText)
        content_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        content_lbl.setWordWrap(True)
        content_lbl.setText(
            f"<div style='color: {TEXT_PRIMARY}; font-size: 13px; line-height: 1.45;'>{formatted_html}</div>"
        )
        bubble_layout.addWidget(content_lbl)

        # Assemble layout with alignment
        if is_user:
            root.addStretch()
            root.addWidget(bubble, stretch=4)
            root.addWidget(avatar_lbl, alignment=Qt.AlignmentFlag.AlignTop)
        else:
            root.addWidget(avatar_lbl, alignment=Qt.AlignmentFlag.AlignTop)
            root.addWidget(bubble, stretch=4)
            root.addStretch()


class EmptyConversationWidget(QWidget):
    """Empty state widget shown when an agent has no historical messages."""

    def __init__(self, agent_name: str, description: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 60, 20, 40)

        icon = QLabel("💬")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 32px;")
        layout.addWidget(icon)

        title = QLabel(f"Start a conversation with {agent_name}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT_PRIMARY};")
        layout.addWidget(title)

        desc = QLabel(description or "Type your instructions or questions below to begin.")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"font-size: 12px; color: {TEXT_MUTED}; max-width: 420px;")
        layout.addWidget(desc)


class FridayChatWidget(QFrame):
    """Central conversation panel integrating chat and input."""

    message_sent = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {BG_MAIN}; border: 1px solid {BORDER_SUBTLE}; border-radius: 8px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Message List
        self.msg_list = QListWidget()
        self.msg_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.msg_list.setStyleSheet(f"background-color: {BG_MAIN}; border: none; padding: 8px;")
        layout.addWidget(self.msg_list, stretch=1)

        # Input Area
        input_container = QWidget()
        input_container.setStyleSheet(f"background-color: {BG_CARD}; border-top: 1px solid {BORDER_SUBTLE};")
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(10, 8, 10, 8)
        input_layout.setSpacing(8)

        self.input_edit = ChatInputEdit()
        self.input_edit.setPlaceholderText("Message agent… (Enter to send, Shift+Enter for newline)")
        self.input_edit.setFixedHeight(46)
        self.input_edit.send_requested.connect(self._on_send_clicked)

        self.send_btn = QPushButton("Send")
        self.send_btn.setProperty("class", "btn-primary")
        self.send_btn.setFixedHeight(46)
        self.send_btn.clicked.connect(self._on_send_clicked)

        input_layout.addWidget(self.input_edit, stretch=1)
        input_layout.addWidget(self.send_btn)

        layout.addWidget(input_container)

    def add_message(self, role: str, text: str, timestamp: str = "") -> None:
        """Add a message bubble to the list with proper dynamic size."""
        if not timestamp:
            from datetime import datetime
            timestamp = datetime.now().strftime("%I:%M %p")

        item = QListWidgetItem()
        widget = MessageItemWidget(role, text, timestamp)
        self.msg_list.addItem(item)
        self.msg_list.setItemWidget(item, widget)
        item.setSizeHint(widget.sizeHint())
        self.msg_list.scrollToBottom()

    def set_typing(self, is_typing: bool) -> None:
        self.send_btn.setEnabled(not is_typing)
        if not is_typing:
            self.input_edit.setFocus()

    def _on_send_clicked(self) -> None:
        text = self.input_edit.toPlainText().strip()
        if not text:
            return
        self.input_edit.clear()
        self.message_sent.emit(text)
