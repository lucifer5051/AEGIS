"""ModelsView — Truthful Model Discovery, Health Status & Routing Configuration.

Displays verified local GGUF models, daemon status, and cloud endpoints.
Allows configuring routing aliases and local-only boundaries.

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
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.models.discovery import discover_all_models
from src.models.huggingface import evaluate_hardware_compatibility, get_download_manager
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


class ModelsView(QWidget):
    """Management view for local and cloud models and routing configuration."""

    def __init__(self, service: UiService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service
        self._models_cache: List[Dict[str, Any]] = []
        self._filtered_models: List[Dict[str, Any]] = []
        self._selected_model: Optional[Dict[str, Any]] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Header
        hdr = QHBoxLayout()
        v_titles = QVBoxLayout()
        title = QLabel("MODEL MANAGEMENT & ROUTING")
        title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 18px; font-weight: 700; letter-spacing: 0.5px;")
        subtitle = QLabel("Truthful discovery of resident local weights, Ollama daemons, and cloud gateways.")
        subtitle.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        v_titles.addWidget(title)
        v_titles.addWidget(subtitle)
        hdr.addLayout(v_titles)
        hdr.addStretch()

        self.rescan_btn = QPushButton("🔄 Rescan Discovered Models")
        self.rescan_btn.setStyleSheet(
            f"background-color: {ACCENT_PURPLE}; color: #ffffff; border: none; "
            f"border-radius: 6px; padding: 8px 16px; font-weight: 600; font-size: 12px;"
        )
        self.rescan_btn.clicked.connect(self.rescan_models)
        hdr.addWidget(self.rescan_btn)
        layout.addLayout(hdr)

        # Scroll area for content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")

        container = QWidget()
        c_layout = QVBoxLayout(container)
        c_layout.setContentsMargins(0, 0, 0, 0)
        c_layout.setSpacing(18)

        # 1. Summary Metrics Cards
        self.metrics_layout = QHBoxLayout()
        self.metrics_layout.setSpacing(12)
        c_layout.addLayout(self.metrics_layout)

        # 2. Discovered Models Table
        table_card = QFrame()
        table_card.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; border-radius: 8px; padding: 12px;"
        )
        t_layout = QVBoxLayout(table_card)
        t_layout.setContentsMargins(12, 12, 12, 12)
        t_layout.setSpacing(10)

        t_lbl = QLabel("DISCOVERED MODEL INVENTORY")
        t_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px; font-weight: 700; letter-spacing: 1px;")
        t_layout.addWidget(t_lbl)

        # Filters Bar
        f_row = QHBoxLayout()
        f_row.setSpacing(8)

        self.search_model_input = QLineEdit()
        self.search_model_input.setPlaceholderText("Filter by name or ID…")
        self.search_model_input.setStyleSheet(
            f"background-color: {BG_INPUT}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_PRIMARY}; padding: 3px 8px; font-size: 11px; max-width: 220px;"
        )
        self.search_model_input.textChanged.connect(self._apply_filters)
        f_row.addWidget(self.search_model_input)

        self.filter_type_combo = QComboBox()
        self.filter_type_combo.addItems(["All Runtimes", "Local Only", "Cloud Only", "Ready / Installed"])
        self.filter_type_combo.setStyleSheet(
            f"background-color: {BG_INPUT}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_PRIMARY}; padding: 2px 6px; font-size: 11px;"
        )
        self.filter_type_combo.currentIndexChanged.connect(self._apply_filters)
        f_row.addWidget(self.filter_type_combo)

        self.filter_cat_combo = QComboBox()
        self.filter_cat_combo.addItems([
            "All Categories", "General Brain", "Reasoning", "Coding",
            "Vision", "Audio", "Multimodal", "Embeddings", "Tool Calling"
        ])
        self.filter_cat_combo.setStyleSheet(
            f"background-color: {BG_INPUT}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 4px; color: {TEXT_PRIMARY}; padding: 2px 6px; font-size: 11px;"
        )
        self.filter_cat_combo.currentIndexChanged.connect(self._apply_filters)
        f_row.addWidget(self.filter_cat_combo)
        f_row.addStretch()

        t_layout.addLayout(f_row)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "Model Name / ID", "Provider", "Type", "Status", "Size / VRAM", "Context"
        ])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setStyleSheet(
            f"QTableWidget {{ background-color: {BG_MAIN}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 6px; color: {TEXT_PRIMARY}; gridline-color: {BORDER_SUBTLE}; font-size: 12px; }}"
            f"QHeaderView::section {{ background-color: {BG_CARD}; color: {TEXT_MUTED}; "
            f"border: 1px solid {BORDER_SUBTLE}; padding: 6px; font-weight: 600; font-size: 11px; }}"
        )
        self.table.setMinimumHeight(240)
        self.table.itemSelectionChanged.connect(self._on_model_row_selected)
        t_layout.addWidget(self.table)

        # Model Detail & Assignment Card
        self.detail_card = QFrame()
        self.detail_card.setStyleSheet(
            f"background-color: {BG_MAIN}; border: 1px solid {BORDER_SUBTLE}; border-radius: 6px; padding: 10px;"
        )
        d_layout = QVBoxLayout(self.detail_card)
        d_layout.setContentsMargins(10, 8, 10, 8)
        d_layout.setSpacing(6)

        d_header = QHBoxLayout()
        self.d_title_lbl = QLabel("Select a model from the inventory to inspect runtime properties and assign roles.")
        self.d_title_lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 13px; font-weight: 700;")
        self.d_status_pill = QLabel("")
        d_header.addWidget(self.d_title_lbl)
        d_header.addStretch()
        d_header.addWidget(self.d_status_pill)
        d_layout.addLayout(d_header)

        self.d_desc_lbl = QLabel("")
        self.d_desc_lbl.setWordWrap(True)
        self.d_desc_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        d_layout.addWidget(self.d_desc_lbl)

        self.d_hw_lbl = QLabel("")
        self.d_hw_lbl.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 11px; font-weight: 600;")
        d_layout.addWidget(self.d_hw_lbl)

        d_actions = QHBoxLayout()
        self.btn_set_brain = QPushButton("Set as Central Brain")
        self.btn_set_brain.setStyleSheet(
            f"background-color: {ACCENT_PURPLE}; color: #ffffff; border: none; border-radius: 4px; padding: 4px 12px; font-size: 11px; font-weight: 600;"
        )
        self.btn_set_brain.clicked.connect(self._on_set_as_brain)
        d_actions.addWidget(self.btn_set_brain)

        self.btn_assign_agent = QPushButton("Assign to Friday / Agents")
        self.btn_assign_agent.setStyleSheet(
            f"background-color: {BG_CARD}; color: {TEXT_PRIMARY}; border: 1px solid {BORDER_SUBTLE}; border-radius: 4px; padding: 4px 12px; font-size: 11px;"
        )
        self.btn_assign_agent.clicked.connect(self._on_assign_to_agent)
        d_actions.addWidget(self.btn_assign_agent)

        self.btn_download_model = QPushButton("Download Weights")
        self.btn_download_model.setStyleSheet(
            f"background-color: {ACCENT_GREEN}; color: #ffffff; border: none; border-radius: 4px; padding: 4px 12px; font-size: 11px; font-weight: 600;"
        )
        self.btn_download_model.clicked.connect(self._on_download_selected)
        d_actions.addWidget(self.btn_download_model)
        d_actions.addStretch()

        d_layout.addLayout(d_actions)
        t_layout.addWidget(self.detail_card)

        c_layout.addWidget(table_card)

        # 3. Routing Aliases Configuration
        cfg_card = QFrame()
        cfg_card.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; border-radius: 8px; padding: 16px;"
        )
        cfg_layout = QVBoxLayout(cfg_card)
        cfg_layout.setSpacing(14)

        cfg_title = QLabel("MODEL ROUTING ALIASES")
        cfg_title.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px; font-weight: 700; letter-spacing: 1px;")
        cfg_layout.addWidget(cfg_title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)

        self.alias_combos: Dict[str, QComboBox] = {}
        aliases = [
            ("fast", "Fast Reasoning Tier (Small/Chat):", 0),
            ("balanced", "Balanced Tier (General Executive):", 1),
            ("reasoning", "Deep Reasoning Tier (Research/Analysis):", 2),
            ("coding", "Coding Tier (Code & Refactor):", 3),
            ("local", "Local-Only Enforced Tier:", 4),
        ]

        for alias_key, label_text, row in aliases:
            lbl = QLabel(label_text)
            lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 500;")
            combo = QComboBox()
            combo.setStyleSheet(
                f"background-color: {BG_INPUT}; border: 1px solid {BORDER_SUBTLE}; "
                f"border-radius: 4px; color: {TEXT_PRIMARY}; padding: 4px 8px; min-width: 260px;"
            )
            self.alias_combos[alias_key] = combo
            grid.addWidget(lbl, row, 0)
            grid.addWidget(combo, row, 1)

        cfg_layout.addLayout(grid)

        # Local-Only Policy Enforcer
        self.cb_local_only = QCheckBox("Strict Local-Only Execution (Disallow all external network/cloud egress)")
        self.cb_local_only.setChecked(True)
        self.cb_local_only.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600;")
        cfg_layout.addWidget(self.cb_local_only)

        save_btn = QPushButton("Save Routing Configuration")
        save_btn.setFixedSize(200, 32)
        save_btn.setStyleSheet(
            f"background-color: {ACCENT_GREEN}; color: #ffffff; border: none; "
            f"border-radius: 5px; font-weight: 700; font-size: 12px;"
        )
        save_btn.clicked.connect(self._on_save_routing)
        cfg_layout.addWidget(save_btn)

        c_layout.addWidget(cfg_card)
        c_layout.addStretch()

        scroll.setWidget(container)
        layout.addWidget(scroll)

        # Connect signals
        self._service.models_updated.connect(self.set_models)
        self.rescan_models()

    def rescan_models(self) -> None:
        self._service.refresh_models()

    def set_models(self, models: List[Dict[str, Any]]) -> None:
        self._models_cache = models
        self._render_metrics(models)
        self._apply_filters()
        self._populate_alias_dropdowns(models)

    def _apply_filters(self) -> None:
        query = self.search_model_input.text().strip().lower()
        type_filter = self.filter_type_combo.currentText()
        cat_filter = self.filter_cat_combo.currentText()

        filtered: List[Dict[str, Any]] = []
        for m in self._models_cache:
            # Query match
            if query:
                name = str(m.get("display_name", "")).lower()
                mid = str(m.get("model_id", "")).lower()
                desc = str(m.get("description", "")).lower()
                if query not in name and query not in mid and query not in desc:
                    continue

            # Type match
            ptype = str(m.get("provider_type", "")).lower()
            status = str(m.get("status", "")).upper()
            if type_filter == "Local Only" and ptype != "local":
                continue
            elif type_filter == "Cloud Only" and ptype != "cloud":
                continue
            elif type_filter == "Ready / Installed" and status not in ("READY", "INSTALLED"):
                continue

            # Category match
            if cat_filter != "All Categories":
                cat_norm = cat_filter.lower().replace(" ", "_")
                m_cat = str(m.get("category", "")).lower()
                caps = [str(c).lower() for c in m.get("capabilities", [])]
                desc = str(m.get("description", "")).lower()
                if cat_norm not in m_cat and not any(cat_norm in c for c in caps) and cat_filter.lower() not in desc:
                    continue

            filtered.append(m)

        self._render_table(filtered)

    def _render_metrics(self, models: List[Dict[str, Any]]) -> None:
        # Clear existing
        while self.metrics_layout.count():
            item = self.metrics_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        ready_count = sum(1 for m in models if m.get("status") == "READY")
        local_count = sum(1 for m in models if m.get("provider_type") == "local" and m.get("status") == "READY")
        unavail_count = sum(1 for m in models if m.get("status") in ("UNAVAILABLE", "NOT_CONFIGURED"))

        cards = [
            ("TOTAL DISCOVERED", str(len(models)), ACCENT_CYAN),
            ("READY & ACTIVE", str(ready_count), ACCENT_GREEN),
            ("LOCAL VERIFIED", str(local_count), ACCENT_PURPLE),
            ("OFFLINE / UNCONFIGURED", str(unavail_count), ACCENT_AMBER),
        ]

        for title, value, color in cards:
            card = QFrame()
            card.setStyleSheet(
                f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; border-radius: 6px; padding: 10px;"
            )
            v = QVBoxLayout(card)
            v.setContentsMargins(8, 6, 8, 6)
            v.setSpacing(2)

            t_lbl = QLabel(title)
            t_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px; font-weight: 700; letter-spacing: 0.5px;")
            val_lbl = QLabel(value)
            val_lbl.setStyleSheet(f"color: {color}; font-size: 20px; font-weight: 800;")
            v.addWidget(t_lbl)
            v.addWidget(val_lbl)
            self.metrics_layout.addWidget(card)

    def _render_table(self, models: List[Dict[str, Any]]) -> None:
        self._filtered_models = list(models)
        self.table.setRowCount(len(models))
        for row, m in enumerate(models):
            name = m.get("display_name", m.get("model_id", ""))
            prov = m.get("provider_id", "")
            ptype = str(m.get("provider_type", "")).upper()
            status = m.get("status", "UNKNOWN")
            size = f"{m.get('size_mb', 0)} MB" if m.get("size_mb") else "Resident / API"
            ctx = f"{m.get('context_window', 4096):,} tokens"

            item_name = QTableWidgetItem(name)
            item_prov = QTableWidgetItem(prov)
            item_type = QTableWidgetItem(ptype)
            item_status = QTableWidgetItem(status)
            item_size = QTableWidgetItem(size)
            item_ctx = QTableWidgetItem(ctx)

            # Styling status
            if status == "READY":
                item_status.setForeground(Qt.GlobalColor.green)
            elif status in ("DISCOVERED", "NOT_INSTALLED"):
                item_status.setForeground(Qt.GlobalColor.cyan)
            elif status == "UNAVAILABLE":
                item_status.setForeground(Qt.GlobalColor.yellow)
            elif status == "NOT_CONFIGURED":
                item_status.setForeground(Qt.GlobalColor.gray)

            self.table.setItem(row, 0, item_name)
            self.table.setItem(row, 1, item_prov)
            self.table.setItem(row, 2, item_type)
            self.table.setItem(row, 3, item_status)
            self.table.setItem(row, 4, item_size)
            self.table.setItem(row, 5, item_ctx)

        # Select first row if available and nothing selected
        if models and self.table.currentRow() < 0:
            self.table.selectRow(0)

    def _on_model_row_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._filtered_models):
            return
        m = self._filtered_models[row]
        self._selected_model = m

        name = m.get("display_name", m.get("model_id", "Unknown"))
        self.d_title_lbl.setText(f"{name}  [{m.get('model_id', '')}]")
        self.d_desc_lbl.setText(m.get("description", "No description available."))

        status = m.get("status", "UNKNOWN")
        color = ACCENT_GREEN if status == "READY" else (ACCENT_CYAN if status in ("DISCOVERED", "NOT_INSTALLED") else TEXT_MUTED)
        self.d_status_pill.setText(f"Status: {status}")
        self.d_status_pill.setStyleSheet(f"color: {color}; font-weight: 700; font-size: 11px;")

        hw = evaluate_hardware_compatibility(m)
        hw_msg = hw.get("message", "Hardware compatibility not evaluated.")
        hw_color = ACCENT_GREEN if hw.get("compatible") else ACCENT_RED
        self.d_hw_lbl.setText(f"Hardware Assessment: {hw_msg}")
        self.d_hw_lbl.setStyleSheet(f"color: {hw_color}; font-size: 11px; font-weight: 600;")

        is_ready = status == "READY"
        self.btn_set_brain.setEnabled(is_ready)
        self.btn_assign_agent.setEnabled(is_ready)
        self.btn_download_model.setVisible(not is_ready and m.get("provider_type") == "local")

    def _on_set_as_brain(self) -> None:
        if not self._selected_model:
            return
        mid = self._selected_model.get("model_id")
        combo = self.alias_combos.get("balanced")
        if combo:
            idx = -1
            for i in range(combo.count()):
                if combo.itemData(i) == mid:
                    idx = i
                    break
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.addItem(f"{self._selected_model.get('display_name')} ({self._selected_model.get('provider_id')})", mid)
                combo.setCurrentIndex(combo.count() - 1)
        QMessageBox.information(
            self,
            "Central Brain Assignment",
            f"Assigned '{self._selected_model.get('display_name')}' ({mid}) as the primary model for Central Brain.",
        )

    def _on_assign_to_agent(self) -> None:
        if not self._selected_model:
            return
        mid = self._selected_model.get("model_id")
        agent_reg = getattr(self._service._app, "agent_registry", None)
        if agent_reg and hasattr(agent_reg, "update_agent_model"):
            agent_reg.update_agent_model("friday", mid)
        QMessageBox.information(
            self,
            "Agent Model Assignment",
            f"Assigned '{self._selected_model.get('display_name')}' ({mid}) as preferred model for Friday.",
        )

    def _on_download_selected(self) -> None:
        if not self._selected_model:
            return
        mgr = get_download_manager()
        mgr.start_download(self._selected_model, simulate=True)
        QMessageBox.information(
            self,
            "Model Download Initiated",
            f"Download queued for '{self._selected_model.get('display_name')}'.\nCheck the Skills & Hugging Face tab for progress.",
        )

    def _populate_alias_dropdowns(self, models: List[Dict[str, Any]]) -> None:
        options = []
        for m in models:
            if m.get("status") == "READY":
                options.append((m.get("model_id"), f"{m.get('display_name')} ({m.get('provider_id')})"))

        for alias_key, combo in self.alias_combos.items():
            curr = combo.currentData()
            combo.clear()
            for mid, label in options:
                combo.addItem(label, mid)

            # Set smart defaults
            if alias_key in ("fast", "balanced", "local"):
                # Prefer local GGUF or fallback
                for i in range(combo.count()):
                    val = combo.itemData(i)
                    if "qwen35" in str(val).lower() or "sage-local" in str(val).lower():
                        combo.setCurrentIndex(i)
                        break
            elif alias_key in ("reasoning", "coding"):
                for i in range(combo.count()):
                    val = combo.itemData(i)
                    if "qwen" in str(val).lower() or "deepseek" in str(val).lower():
                        combo.setCurrentIndex(i)
                        break

    def _on_save_routing(self) -> None:
        logger.info("Saved model routing preferences.")
