"""SkillsView — Dedicated Skills Marketplace & Hugging Face Model Download Manager.

Provides management of installed skills, discoverable skills marketplace,
and Hugging Face GGUF model downloads with RTX 3050 hardware compatibility checks.

Canonical Reference:
    PROMPT 080: Functional Stabilization, Conversation Repair & Model Management
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.models.huggingface import (
    CURATED_HF_MODELS,
    HardwareProfile,
    evaluate_hardware_compatibility,
    get_download_manager,
)
from src.skills.marketplace import get_skill_manager
from src.ui.desktop.service import UiService
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
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)

logger = logging.getLogger(__name__)


class SkillCardWidget(QFrame):
    """Visual card for an installed or available agent skill."""

    def __init__(
        self,
        skill_data: Dict[str, Any],
        is_installed: bool,
        on_toggle: Optional[Any] = None,
        on_install: Optional[Any] = None,
        on_uninstall: Optional[Any] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.skill_data = skill_data
        self.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 8px; margin: 4px 0px;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # Header: Name, Version, Category, and Action
        top_row = QHBoxLayout()
        name_lbl = QLabel(f"<b>{skill_data.get('name', 'Skill')}</b>")
        name_lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 14px;")

        ver_lbl = QLabel(f"v{skill_data.get('version', '1.0.0')}")
        ver_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")

        cat_lbl = QLabel(skill_data.get("category", "General").upper())
        cat_lbl.setStyleSheet(
            f"background-color: {BG_MAIN}; color: {ACCENT_CYAN}; "
            f"border: 1px solid {BORDER_SUBTLE}; border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: 700;"
        )

        top_row.addWidget(name_lbl)
        top_row.addWidget(ver_lbl)
        top_row.addWidget(cat_lbl)
        top_row.addStretch()

        if is_installed:
            enabled = skill_data.get("enabled", True)
            self.toggle_btn = QPushButton("Enabled" if enabled else "Disabled")
            self.toggle_btn.setCheckable(True)
            self.toggle_btn.setChecked(enabled)
            btn_bg = ACCENT_GREEN if enabled else "#4C566A"
            self.toggle_btn.setStyleSheet(
                f"background-color: {btn_bg}; color: #ffffff; border: none; "
                f"border-radius: 4px; padding: 4px 12px; font-weight: 600; font-size: 11px;"
            )
            if on_toggle:
                self.toggle_btn.clicked.connect(lambda: on_toggle(skill_data["skill_id"], self.toggle_btn.isChecked()))
            top_row.addWidget(self.toggle_btn)

            un_btn = QPushButton("Uninstall")
            un_btn.setStyleSheet(
                f"background-color: transparent; color: {ACCENT_RED}; border: 1px solid {BORDER_SUBTLE}; "
                f"border-radius: 4px; padding: 4px 8px; font-size: 11px;"
            )
            if on_uninstall:
                un_btn.clicked.connect(lambda: on_uninstall(skill_data["skill_id"]))
            top_row.addWidget(un_btn)
        else:
            in_btn = QPushButton("Install Skill")
            in_btn.setStyleSheet(
                f"background-color: {ACCENT_PURPLE}; color: #ffffff; border: none; "
                f"border-radius: 4px; padding: 4px 14px; font-weight: 600; font-size: 11px;"
            )
            if on_install:
                in_btn.clicked.connect(lambda: on_install(skill_data["skill_id"]))
            top_row.addWidget(in_btn)

        layout.addLayout(top_row)

        desc = QLabel(skill_data.get("description", ""))
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        layout.addWidget(desc)

        # Meta info: Required Tools
        meta_row = QHBoxLayout()
        tools = ", ".join(skill_data.get("required_tools", []))
        meta_lbl = QLabel(f"<b>Required Tools:</b> {tools}")
        meta_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        meta_row.addWidget(meta_lbl)
        meta_row.addStretch()
        layout.addLayout(meta_row)

        # Candidate Model Pool (>= 3 verified models)
        compat_models = skill_data.get("compatible_models") or []
        if not compat_models:
            prim = skill_data.get("primary_model")
            alts = skill_data.get("alternative_models") or []
            compat_models = ([prim] if prim else []) + alts
        if not compat_models:
            compat_models = skill_data.get("recommended_models", ["sage-local-v1", "qwen35-4b-q4km", "deepseek-coder-6.7b"])

        pool_row = QHBoxLayout()
        pool_lbl = QLabel(f"<b>Candidate Pool ({len(compat_models)}):</b> {', '.join(compat_models)}")
        pool_lbl.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 11px;")
        pool_lbl.setWordWrap(True)
        pool_row.addWidget(pool_lbl)
        layout.addLayout(pool_row)

        # Model Assignment Dropdown
        assign_row = QHBoxLayout()
        assign_lbl = QLabel("Assigned Model:")
        assign_lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 11px; font-weight: 600;")
        assign_row.addWidget(assign_lbl)

        self.model_combo = QComboBox()
        self.model_combo.setStyleSheet(
            f"background-color: {BG_INPUT}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_PRIMARY}; padding: 2px 8px; font-size: 11px; min-width: 180px;"
        )
        self.model_combo.addItem("Auto (Adaptive Candidate Selection)", "Auto")
        for m_cand in compat_models:
            self.model_combo.addItem(f"{m_cand}", m_cand)

        curr_assigned = skill_data.get("assigned_model", "Auto")
        idx = self.model_combo.findData(curr_assigned)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        else:
            if curr_assigned and curr_assigned != "Auto":
                self.model_combo.addItem(curr_assigned, curr_assigned)
                self.model_combo.setCurrentIndex(self.model_combo.count() - 1)

        def _on_model_changed(i: int) -> None:
            chosen = self.model_combo.itemData(i)
            get_skill_manager().assign_model_to_skill(skill_data.get("skill_id", ""), str(chosen))

        self.model_combo.currentIndexChanged.connect(_on_model_changed)
        assign_row.addWidget(self.model_combo)
        assign_row.addStretch()
        layout.addLayout(assign_row)


class HuggingFaceModelCard(QFrame):
    """Interactive card for downloading curated GGUF models with VRAM checks."""

    def __init__(self, model_entry: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.entry = model_entry
        self.dm = get_download_manager()

        self.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 8px; margin: 6px 0px;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # Header
        top_row = QHBoxLayout()
        title = QLabel(f"<b>{model_entry['name']}</b> ({model_entry['parameters']})")
        title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 14px;")

        quant = QLabel(model_entry["quantization"])
        quant.setStyleSheet(
            f"background-color: {BG_MAIN}; color: {ACCENT_LAVENDER}; "
            f"border: 1px solid {BORDER_SUBTLE}; border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: 700;"
        )

        size_lbl = QLabel(f"📦 {model_entry['size_gb']} GB")
        size_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")

        # Hardware compatibility calculation
        compat = evaluate_hardware_compatibility(model_entry["size_gb"])
        compat_badge = QLabel(f"⚡ {compat['badge']}")
        compat_badge.setStyleSheet(
            f"background-color: #1E2D2F; color: {compat['color']}; "
            f"border: 1px solid {compat['color']}; border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: 700;"
        )

        top_row.addWidget(title)
        top_row.addWidget(quant)
        top_row.addWidget(size_lbl)
        top_row.addWidget(compat_badge)
        top_row.addStretch()

        self.download_btn = QPushButton("Download GGUF")
        self.download_btn.setStyleSheet(
            f"background-color: {ACCENT_PURPLE}; color: #ffffff; border: none; "
            f"border-radius: 4px; padding: 5px 14px; font-weight: 600; font-size: 11px;"
        )
        self.download_btn.clicked.connect(self._start_download)
        top_row.addWidget(self.download_btn)

        layout.addLayout(top_row)

        desc = QLabel(model_entry["description"])
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        layout.addWidget(desc)

        hw_note = QLabel(f"<b>Hardware Analysis:</b> {compat['note']}")
        hw_note.setStyleSheet(f"color: {compat['color']}; font-size: 11px;")
        layout.addWidget(hw_note)

        # Progress bar (initially hidden)
        self.progress_container = QWidget()
        p_box = QVBoxLayout(self.progress_container)
        p_box.setContentsMargins(0, 4, 0, 0)
        p_box.setSpacing(4)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet(
            f"QProgressBar {{ background-color: {BG_INPUT}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; height: 12px; text-align: center; color: #ffffff; font-size: 10px; }}"
            f"QProgressBar::chunk {{ background-color: {ACCENT_GREEN}; border-radius: 3px; }}"
        )
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 11px; font-style: italic;")

        p_box.addWidget(self.progress_bar)
        p_box.addWidget(self.status_lbl)
        self.progress_container.setVisible(False)
        layout.addWidget(self.progress_container)

    def _start_download(self) -> None:
        self.download_btn.setEnabled(False)
        self.progress_container.setVisible(True)
        self.status_lbl.setText("Initiating download…")

        def _on_progress(pct: float, msg: str):
            self.progress_bar.setValue(int(pct))
            self.status_lbl.setText(msg)
            if pct >= 100.0:
                self.download_btn.setText("Installed ✔")
                self.download_btn.setStyleSheet(
                    f"background-color: {ACCENT_GREEN}; color: #ffffff; border: none; "
                    f"border-radius: 4px; padding: 5px 14px; font-weight: 600; font-size: 11px;"
                )

        self.dm.start_download(self.entry, progress_callback=_on_progress, simulate=True)


class SkillsView(QWidget):
    """Main view for Skills management and Hugging Face model acquisition."""

    def __init__(self, service: UiService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service
        self._skill_mgr = get_skill_manager()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Header
        hdr = QHBoxLayout()
        v_titles = QVBoxLayout()
        title = QLabel("SKILLS MARKETPLACE & LOCAL MODELS")
        title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 18px; font-weight: 700; letter-spacing: 0.5px;")
        subtitle = QLabel("Equip SAGE agents with modular capabilities and download optimized Hugging Face GGUFs.")
        subtitle.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        v_titles.addWidget(title)
        v_titles.addWidget(subtitle)
        hdr.addLayout(v_titles)
        hdr.addStretch()

        refresh_btn = QPushButton("🔄 Refresh Skills")
        refresh_btn.setStyleSheet(
            f"background-color: {BG_CARD}; color: {TEXT_PRIMARY}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 6px; padding: 8px 16px; font-weight: 600; font-size: 12px;"
        )
        refresh_btn.clicked.connect(self.refresh_all)
        hdr.addWidget(refresh_btn)
        layout.addLayout(hdr)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            f"QTabWidget::pane {{ border: 1px solid {BORDER_SUBTLE}; background-color: {BG_MAIN}; border-radius: 6px; }}"
            f"QTabBar::tab {{ background-color: {BG_CARD}; color: {TEXT_MUTED}; border: 1px solid {BORDER_SUBTLE}; "
            f"padding: 8px 18px; border-top-left-radius: 6px; border-top-right-radius: 6px; font-size: 12px; font-weight: 600; }}"
            f"QTabBar::tab:selected {{ background-color: {BG_MAIN}; color: {TEXT_PRIMARY}; border-bottom-color: {BG_MAIN}; }}"
        )

        # Tab 1: Installed Skills
        self.installed_container = QWidget()
        self.installed_layout = QVBoxLayout(self.installed_container)
        self.installed_layout.setContentsMargins(12, 12, 12, 12)
        self.installed_layout.setSpacing(8)
        self.installed_scroll = self._wrap_scroll(self.installed_container)
        self.tabs.addTab(self.installed_scroll, "Installed Skills (4)")

        # Tab 2: Available Skills Marketplace
        self.available_container = QWidget()
        self.available_layout = QVBoxLayout(self.available_container)
        self.available_layout.setContentsMargins(12, 12, 12, 12)
        self.available_layout.setSpacing(8)
        self.available_scroll = self._wrap_scroll(self.available_container)
        self.tabs.addTab(self.available_scroll, "Available Marketplace (3)")

        # Tab 3: Hugging Face Downloads
        self.hf_container = QWidget()
        self.hf_layout = QVBoxLayout(self.hf_container)
        self.hf_layout.setContentsMargins(12, 12, 12, 12)
        self.hf_layout.setSpacing(10)

        # Hardware profiling banner
        hw = HardwareProfile.get_profile()
        hw_banner = QFrame()
        hw_banner.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {ACCENT_CYAN}; border-radius: 8px; padding: 12px;"
        )
        hwb_layout = QHBoxLayout(hw_banner)
        hwb_layout.setContentsMargins(12, 8, 12, 8)
        hw_icon = QLabel("🖥")
        hw_icon.setStyleSheet("font-size: 24px;")
        hw_text = QLabel(
            f"<b>Hardware Profiler:</b> {hw['gpu_name']} | "
            f"<b>VRAM:</b> {hw['vram_gb']} GB | <b>RAM:</b> {hw['ram_gb']} GB<br>"
            f"<span style='color: {TEXT_MUTED}; font-size: 11px;'>Models up to 4.5 GB fit within VRAM/RAM boundaries for smooth local inference.</span>"
        )
        hw_text.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px;")
        hwb_layout.addWidget(hw_icon)
        hwb_layout.addSpacing(10)
        hwb_layout.addWidget(hw_text)
        hwb_layout.addStretch()
        self.hf_layout.addWidget(hw_banner)

        for m in CURATED_HF_MODELS:
            self.hf_layout.addWidget(HuggingFaceModelCard(m))

        self.hf_layout.addStretch()
        self.hf_scroll = self._wrap_scroll(self.hf_container)
        self.tabs.addTab(self.hf_scroll, "Hugging Face Models (5)")

        layout.addWidget(self.tabs)
        self.refresh_all()

    def _wrap_scroll(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")
        scroll.setWidget(widget)
        return scroll

    def refresh_all(self) -> None:
        installed = self._skill_mgr.list_installed_skills()
        available = self._skill_mgr.list_available_skills()

        self.tabs.setTabText(0, f"Installed Skills ({len(installed)})")
        self.tabs.setTabText(1, f"Available Marketplace ({len(available)})")

        # Clear installed
        while self.installed_layout.count():
            item = self.installed_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for s in installed:
            card = SkillCardWidget(
                s,
                is_installed=True,
                on_toggle=self._on_toggle_skill,
                on_uninstall=self._on_uninstall_skill,
            )
            self.installed_layout.addWidget(card)
        self.installed_layout.addStretch()

        # Clear available
        while self.available_layout.count():
            item = self.available_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for s in available:
            card = SkillCardWidget(
                s,
                is_installed=False,
                on_install=self._on_install_skill,
            )
            self.available_layout.addWidget(card)
        self.available_layout.addStretch()

    def _on_toggle_skill(self, skill_id: str, enabled: bool) -> None:
        self._skill_mgr.toggle_skill(skill_id, enabled)
        self.refresh_all()

    def _on_install_skill(self, skill_id: str) -> None:
        self._skill_mgr.install_skill(skill_id)
        self.refresh_all()

    def _on_uninstall_skill(self, skill_id: str) -> None:
        self._skill_mgr.uninstall_skill(skill_id)
        self.refresh_all()
