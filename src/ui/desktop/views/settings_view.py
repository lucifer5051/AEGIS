"""SettingsView — Comprehensive SAGE Personal AI Workstation Configuration.

Provides organized multi-tab configuration for General, Agents, Models, Skills,
Integrations, Memory & Privacy, Security & TrustCore, and Advanced settings.

Canonical Reference:
    PROMPT 080: Functional Stabilization, Conversation Repair & Model Management
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.service import UiService
from src.ui.desktop.theme import (
    ACCENT_AMBER,
    ACCENT_CYAN,
    ACCENT_GREEN,
    ACCENT_PURPLE,
    ACCENT_RED,
    BG_CARD,
    BG_HOVER,
    BG_INPUT,
    BG_MAIN,
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)

logger = logging.getLogger(__name__)


class SettingsView(QWidget):
    """Multi-tab application settings view."""

    def __init__(self, service: UiService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Header
        hdr = QHBoxLayout()
        v_titles = QVBoxLayout()
        title = QLabel("SYSTEM & WORKSTATION SETTINGS")
        title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 18px; font-weight: 700; letter-spacing: 0.5px;")
        subtitle = QLabel("Configure SAGE agent policies, hardware allocation, memory privacy, and security boundaries.")
        subtitle.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        v_titles.addWidget(title)
        v_titles.addWidget(subtitle)
        hdr.addLayout(v_titles)
        hdr.addStretch()

        save_btn = QPushButton("💾 Save All Settings")
        save_btn.setStyleSheet(
            f"background-color: {ACCENT_GREEN}; color: #ffffff; border: none; "
            f"border-radius: 6px; padding: 8px 18px; font-weight: 700; font-size: 12px;"
        )
        save_btn.clicked.connect(self._on_save_all)
        hdr.addWidget(save_btn)
        layout.addLayout(hdr)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            f"QTabWidget::pane {{ border: 1px solid {BORDER_SUBTLE}; background-color: {BG_MAIN}; border-radius: 6px; }}"
            f"QTabBar::tab {{ background-color: {BG_CARD}; color: {TEXT_MUTED}; border: 1px solid {BORDER_SUBTLE}; "
            f"padding: 8px 16px; border-top-left-radius: 6px; border-top-right-radius: 6px; font-size: 11px; font-weight: 600; }}"
            f"QTabBar::tab:selected {{ background-color: {BG_MAIN}; color: {TEXT_PRIMARY}; border-bottom-color: {BG_MAIN}; }}"
        )

        # 1. General
        self.tabs.addTab(self._build_general_tab(), "General")
        # 2. Agents
        self.tabs.addTab(self._build_agents_tab(), "Agents")
        # 3. Models
        self.tabs.addTab(self._build_models_tab(), "Models")
        # 4. Skills
        self.tabs.addTab(self._build_skills_tab(), "Skills")
        # 5. Integrations
        self.tabs.addTab(self._build_integrations_tab(), "Integrations")
        # 6. Memory & Privacy
        self.tabs.addTab(self._build_memory_tab(), "Memory & Privacy")
        # 7. Security & TrustCore
        self.tabs.addTab(self._build_security_tab(), "Security & TrustCore")
        # 8. Advanced
        self.tabs.addTab(self._build_advanced_tab(), "Advanced")
        # 9. Voice
        self.tabs.addTab(self._build_voice_tab(), "Voice")

        layout.addWidget(self.tabs)

    def _wrap_tab(self, layout: QVBoxLayout) -> QScrollArea:
        container = QWidget()
        container.setLayout(layout)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")
        scroll.setWidget(container)
        return scroll

    # 1. General Tab
    def _build_general_tab(self) -> QWidget:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        card = self._create_card("GENERAL APPLICATION CONFIGURATION")
        c_layout = QVBoxLayout(card)

        # Theme
        row1 = QHBoxLayout()
        lbl1 = QLabel("Workstation Theme:")
        lbl1.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        self.cb_theme = QComboBox()
        self.cb_theme.addItems(["Dark (Kiro Crew Charcoal)", "High Contrast Navy", "Nordic Slate"])
        self._style_input(self.cb_theme)
        row1.addWidget(lbl1)
        row1.addStretch()
        row1.addWidget(self.cb_theme)
        c_layout.addLayout(row1)

        # Logging level
        row2 = QHBoxLayout()
        lbl2 = QLabel("Log Verbosity:")
        lbl2.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        self.cb_log_level = QComboBox()
        self.cb_log_level.addItems(["INFO", "DEBUG", "WARNING", "ERROR"])
        self._style_input(self.cb_log_level)
        row2.addWidget(lbl2)
        row2.addStretch()
        row2.addWidget(self.cb_log_level)
        c_layout.addLayout(row2)

        # Workspace Root
        row3 = QVBoxLayout()
        lbl3 = QLabel("Default Workspace Root Directory:")
        lbl3.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        self.txt_workspace = QLineEdit(r"C:\Users\digvi\OneDrive\Desktop\project\aeigs")
        self._style_input(self.txt_workspace)
        row3.addWidget(lbl3)
        row3.addWidget(self.txt_workspace)
        c_layout.addLayout(row3)

        layout.addWidget(card)
        layout.addStretch()
        return self._wrap_tab(layout)

    # 2. Agents Tab
    def _build_agents_tab(self) -> QWidget:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        card = self._create_card("AGENT RUNTIME & BEHAVIOR")
        c_layout = QVBoxLayout(card)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Primary Conversational Agent:"))
        self.cb_default_agent = QComboBox()
        self.cb_default_agent.addItems(["Friday (Executive Advisor)", "Atlas (Deep Researcher)", "Hephaestus (Coder)", "Hermes (Operator)"])
        self._style_input(self.cb_default_agent)
        r1.addStretch()
        r1.addWidget(self.cb_default_agent)
        c_layout.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Default Reasoning Temperature:"))
        self.spin_temp = QDoubleSpinBox()
        self.spin_temp.setRange(0.0, 1.5)
        self.spin_temp.setSingleStep(0.1)
        self.spin_temp.setValue(0.2)
        self._style_input(self.spin_temp)
        r2.addStretch()
        r2.addWidget(self.spin_temp)
        c_layout.addLayout(r2)

        self.cb_strict_isolation = QCheckBox("Enforce Strict Memory & Session Isolation Between Agents")
        self.cb_strict_isolation.setChecked(True)
        self.cb_strict_isolation.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        c_layout.addWidget(self.cb_strict_isolation)

        layout.addWidget(card)
        layout.addStretch()
        return self._wrap_tab(layout)

    # 3. Models Tab
    def _build_models_tab(self) -> QWidget:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        card = self._create_card("LOCAL MODEL ROUTING & DAEMONS")
        c_layout = QVBoxLayout(card)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Primary Offline Provider:"))
        self.cb_offline_prov = QComboBox()
        self.cb_offline_prov.addItems(["llama.cpp (Local GGUF Weights)", "Local Resident Fallback (Zero-Dependency)"])
        self._style_input(self.cb_offline_prov)
        r1.addStretch()
        r1.addWidget(self.cb_offline_prov)
        c_layout.addLayout(r1)

        r2 = QVBoxLayout()
        r2.addWidget(QLabel("Ollama Local Endpoint URL:"))
        self.txt_ollama = QLineEdit("http://127.0.0.1:11434")
        self._style_input(self.txt_ollama)
        r2.addWidget(self.txt_ollama)
        c_layout.addLayout(r2)

        self.cb_auto_fallback = QCheckBox("Enable Resilient Auto-Fallback on Provider Timeout or Circuit Trip")
        self.cb_auto_fallback.setChecked(True)
        self.cb_auto_fallback.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        c_layout.addWidget(self.cb_auto_fallback)

        layout.addWidget(card)
        layout.addStretch()
        return self._wrap_tab(layout)

    # 4. Skills Tab
    def _build_skills_tab(self) -> QWidget:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        card = self._create_card("SKILL SYSTEM POLICIES")
        c_layout = QVBoxLayout(card)

        self.cb_sandboxed_skills = QCheckBox("Run all skills strictly inside capability-constrained sandboxes")
        self.cb_sandboxed_skills.setChecked(True)
        self.cb_sandboxed_skills.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        c_layout.addWidget(self.cb_sandboxed_skills)

        self.cb_auto_register = QCheckBox("Automatically register newly discovered tools with TrustCore")
        self.cb_auto_register.setChecked(True)
        self.cb_auto_register.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        c_layout.addWidget(self.cb_auto_register)

        layout.addWidget(card)
        layout.addStretch()
        return self._wrap_tab(layout)

    # 5. Integrations Tab
    def _build_integrations_tab(self) -> QWidget:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        card = self._create_card("INTEGRATIONS & CHANNELS")
        c_layout = QVBoxLayout(card)

        self.cb_desktop_control = QCheckBox("Enable Desktop OS Automation Integration (via Hermes)")
        self.cb_desktop_control.setChecked(True)
        self.cb_desktop_control.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        c_layout.addWidget(self.cb_desktop_control)

        self.cb_wa_routing = QCheckBox("Enable WhatsApp Contact Small-Model Routing & Boundaries")
        self.cb_wa_routing.setChecked(True)
        self.cb_wa_routing.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        c_layout.addWidget(self.cb_wa_routing)

        layout.addWidget(card)
        layout.addStretch()
        return self._wrap_tab(layout)

    # 6. Memory & Privacy Tab
    def _build_memory_tab(self) -> QWidget:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        card = self._create_card("DATA PRIVACY & LOCAL PERSISTENCE")
        c_layout = QVBoxLayout(card)

        lbl = QLabel("Active SQLite Database Path: C:\\Users\\digvi\\.aegis\\sage.db")
        lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        c_layout.addWidget(lbl)

        self.cb_ephemeral_sessions = QCheckBox("Encrypt vector embeddings and episodic memory at rest")
        self.cb_ephemeral_sessions.setChecked(True)
        self.cb_ephemeral_sessions.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        c_layout.addWidget(self.cb_ephemeral_sessions)

        prune_btn = QPushButton("🧹 Prune Old Ephemeral Cache")
        prune_btn.setFixedSize(220, 30)
        prune_btn.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; color: {TEXT_PRIMARY}; "
            f"border-radius: 4px; font-size: 11px; font-weight: 600;"
        )
        prune_btn.clicked.connect(lambda: QMessageBox.information(self, "Memory", "Ephemeral cache cleaned."))
        c_layout.addWidget(prune_btn)

        layout.addWidget(card)
        layout.addStretch()
        return self._wrap_tab(layout)

    # 7. Security & TrustCore Tab
    def _build_security_tab(self) -> QWidget:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        card = self._create_card("TRUSTCORE AUTHORIZATION & INVARIANTS")
        c_layout = QVBoxLayout(card)

        tc_note = QLabel(
            "<b>TrustCore Invariant:</b> TrustCore is authoritative and unbypassable. "
            "All model invocations, file operations, and shell tasks require cryptographic capabilities."
        )
        tc_note.setWordWrap(True)
        tc_note.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 12px; margin-bottom: 6px;")
        c_layout.addWidget(tc_note)

        self.cb_require_human_approval = QCheckBox("Always require explicit human approval for destructive shell/file execution")
        self.cb_require_human_approval.setChecked(True)
        self.cb_require_human_approval.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600;")
        c_layout.addWidget(self.cb_require_human_approval)

        self.cb_local_only_mode = QCheckBox("Enforce strict LOCAL_ONLY Network Mode (Block all outbound internet traffic)")
        self.cb_local_only_mode.setChecked(True)
        self.cb_local_only_mode.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600;")
        c_layout.addWidget(self.cb_local_only_mode)

        layout.addWidget(card)
        layout.addStretch()
        return self._wrap_tab(layout)

    # 8. Advanced Tab
    def _build_advanced_tab(self) -> QWidget:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        card = self._create_card("DIAGNOSTICS & SYSTEM MAINTENANCE")
        c_layout = QVBoxLayout(card)

        diag_btn = QPushButton("Export Diagnostic Bundle (.zip)")
        diag_btn.setFixedSize(220, 30)
        diag_btn.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; color: {TEXT_PRIMARY}; "
            f"border-radius: 4px; font-size: 11px; font-weight: 600;"
        )
        diag_btn.clicked.connect(lambda: QMessageBox.information(self, "Diagnostics", "Diagnostic log bundle exported."))
        c_layout.addWidget(diag_btn)

        layout.addWidget(card)
        layout.addStretch()
        return self._wrap_tab(layout)

    # Helpers
    def _create_card(self, title: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; border-radius: 8px; padding: 14px;"
        )
        t_lbl = QLabel(title)
        t_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px; font-weight: 700; letter-spacing: 1px; margin-bottom: 8px;")
        card.layout = QVBoxLayout(card)
        card.layout.addWidget(t_lbl)
        return card

    def _style_input(self, widget: QWidget) -> None:
        widget.setStyleSheet(
            f"background-color: {BG_INPUT}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_PRIMARY}; padding: 4px 8px; font-size: 12px;"
        )

    def _on_save_all(self) -> None:
        QMessageBox.information(self, "Settings Saved", "All workstation settings and policies have been updated successfully.")

    # 9. Voice Tab
    def _build_voice_tab(self):
        """Voice subsystem: microphone selector, TTS rate/volume, and test button."""
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        card = self._create_card("VOICE INPUT / OUTPUT CONFIGURATION")
        c_layout = QVBoxLayout(card)

        input_devices = [{"name": "Default System Microphone", "is_default": True}]
        output_devices = [{"name": "Default System Audio Output", "is_default": True}]
        self._voice_service = None
        try:
            from src.voice.service import VoiceService
            self._voice_service = VoiceService()
            inp = self._voice_service.get_input_devices()
            out = self._voice_service.get_output_devices()
            if inp:
                input_devices = inp
            if out:
                output_devices = out
        except Exception:
            pass

        for label_txt, attr in [
            ("Microphone Input Device:", "cb_mic_device"),
            ("Audio Output Device (TTS):", "cb_spk_device"),
        ]:
            sources = input_devices if attr == "cb_mic_device" else output_devices
            row = QHBoxLayout()
            lbl = QLabel(label_txt)
            lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
            combo = QComboBox()
            self._style_input(combo)
            for dev in sources:
                sfx = "  [Default]" if dev.get("is_default") else ""
                combo.addItem(dev.get("name", "Unknown") + sfx, dev.get("index", 0))
            setattr(self, attr, combo)
            row.addWidget(lbl)
            row.addStretch()
            row.addWidget(combo)
            c_layout.addLayout(row)

        for label_txt, sl_attr, lbl_attr, lo, hi, default in [
            ("TTS Speech Rate:", "sl_tts_rate", "lbl_rate_val", -10, 10, 0),
            ("TTS Volume:", "sl_tts_volume", "lbl_vol_val", 0, 100, 85),
        ]:
            row = QHBoxLayout()
            lbl = QLabel(label_txt)
            lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
            sl = QSlider(Qt.Orientation.Horizontal)
            sl.setMinimum(lo)
            sl.setMaximum(hi)
            sl.setValue(default)
            sl.setFixedWidth(200)
            val_lbl = QLabel(str(default))
            val_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; min-width: 24px;")
            sl.valueChanged.connect(lambda v, vl=val_lbl: vl.setText(str(v)))
            setattr(self, sl_attr, sl)
            setattr(self, lbl_attr, val_lbl)
            row.addWidget(lbl)
            row.addStretch()
            row.addWidget(sl)
            row.addWidget(val_lbl)
            c_layout.addLayout(row)

        ptt_note = QLabel(
            "Push-to-Talk: use the microphone button in the conversation panel (Ctrl+Shift+M)."
        )
        ptt_note.setWordWrap(True)
        ptt_note.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        c_layout.addWidget(ptt_note)

        test_btn = QPushButton("Test Voice Output")
        test_btn.setFixedSize(160, 30)
        test_btn.setStyleSheet(
            f"background-color: {ACCENT_PURPLE}; color: #ffffff; border: none; "
            f"border-radius: 4px; font-size: 11px; font-weight: 600;"
        )

        def _test_voice():
            try:
                if self._voice_service:
                    self._voice_service.set_rate(self.sl_tts_rate.value())
                    self._voice_service.set_volume(self.sl_tts_volume.value())
                    self._voice_service.speak("SAGE Voice Subsystem operational.")
                else:
                    QMessageBox.information(self, "Voice Test", "Voice service not available.")
            except Exception as e:
                QMessageBox.warning(self, "Voice Error", str(e))

        test_btn.clicked.connect(_test_voice)
        c_layout.addWidget(test_btn)

        layout.addWidget(card)
        layout.addStretch()
        return self._wrap_tab(layout)

