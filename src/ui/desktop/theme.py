"""SAGE PySide6 Desktop Theme & Styling — Kiro Crew Inspired Dark Palette.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
"""
from __future__ import annotations

# Kiro Crew High-Contrast Workstation Color Palette
BG_MAIN = "#17151B"          # Main window background
BG_SIDEBAR = "#211D27"       # Navigation sidebar background
BG_CARD = "#24202B"          # Cards, containers, inspector panels
BG_INPUT = "#131117"         # Inputs and editors
BG_HOVER = "#2F2A38"         # Hover state for lists and cards

BORDER_SUBTLE = "#352F3D"    # Subtle dark-purple / gray border
BORDER_FOCUS = "#8B45F5"     # Purple border on focus

TEXT_PRIMARY = "#EDEDEF"     # Bright white / light gray text
TEXT_MUTED = "#9E96AB"       # Muted gray / lavender text
TEXT_DIM = "#6C6478"         # Dim labels and placeholders
TEXT_BRIGHT = "#FFFFFF"      # High contrast white

ACCENT_PURPLE = "#8B45F5"    # Kiro primary accent purple
ACCENT_LAVENDER = "#B78AF7"  # Secondary lighter lavender
ACCENT_CYAN = "#00D2FF"      # Secondary accent for research / status
ACCENT_GREEN = "#10B981"     # Success green
ACCENT_AMBER = "#F59E0B"     # Warning amber
ACCENT_RED = "#EF4444"       # Failure red
ACCENT_GRAY = "#6B7280"      # Neutral / offline gray

# Compatibility aliases for existing component imports
BG_DARK = BG_MAIN
BG_PANEL = BG_CARD
BORDER = BORDER_SUBTLE
TEXT_MAIN = TEXT_PRIMARY
ACCENT_BLUE = ACCENT_CYAN
ACCENT_YELLOW = ACCENT_AMBER
ACCENT_ORANGE = ACCENT_AMBER

DARK_STYLESHEET = f"""
QMainWindow {{
    background-color: {BG_MAIN};
    color: {TEXT_PRIMARY};
}}

QWidget {{
    background-color: {BG_MAIN};
    color: {TEXT_PRIMARY};
    font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, 'Cascadia Code', system-ui, sans-serif;
    font-size: 13px;
}}

QSplitter::handle {{
    background-color: {BORDER_SUBTLE};
    width: 2px;
    height: 2px;
}}

QSplitter::handle:hover {{
    background-color: {ACCENT_PURPLE};
}}

/* Cards and Panels */
QFrame.card {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 8px;
}}

QFrame.panel {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 8px;
}}

/* Header labels */
QLabel.heading {{
    color: {TEXT_PRIMARY};
    font-size: 14px;
    font-weight: 700;
}}

QLabel.subheading {{
    color: {TEXT_MUTED};
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}

/* Lists */
QListWidget {{
    background-color: transparent;
    border: none;
    color: {TEXT_PRIMARY};
    outline: none;
    padding: 2px;
}}

QListWidget::item {{
    padding: 8px 10px;
    border-radius: 6px;
    margin-bottom: 2px;
    background-color: transparent;
    border: 1px solid transparent;
}}

QListWidget::item:hover {{
    background-color: {BG_HOVER};
    border-color: {BORDER_SUBTLE};
}}

QListWidget::item:selected {{
    background-color: #312642;
    border-color: {ACCENT_PURPLE};
    color: {TEXT_PRIMARY};
}}

/* Inputs & Editors */
QTextEdit, QLineEdit {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 6px;
    color: {TEXT_PRIMARY};
    padding: 8px 10px;
    selection-background-color: {ACCENT_PURPLE};
    selection-color: #ffffff;
}}

QTextEdit:focus, QLineEdit:focus {{
    border-color: {ACCENT_PURPLE};
}}

/* Buttons */
QPushButton {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 6px;
    color: {TEXT_PRIMARY};
    padding: 6px 14px;
    font-weight: 600;
    font-size: 12px;
}}

QPushButton:hover {{
    background-color: {BG_HOVER};
    border-color: {ACCENT_PURPLE};
}}

QPushButton:pressed {{
    background-color: #1A1622;
}}

QPushButton:disabled {{
    background-color: {BG_MAIN};
    border-color: {BORDER_SUBTLE};
    color: {TEXT_DIM};
}}

QPushButton.btn-primary {{
    background-color: {ACCENT_PURPLE};
    border: 1px solid {ACCENT_PURPLE};
    color: #ffffff;
}}

QPushButton.btn-primary:hover {{
    background-color: #9C5CF7;
    border-color: #9C5CF7;
}}

QPushButton.btn-danger {{
    background-color: #991B1B;
    border: 1px solid {ACCENT_RED};
    color: #ffffff;
}}

QPushButton.btn-danger:hover {{
    background-color: {ACCENT_RED};
}}

QPushButton.btn-small {{
    padding: 3px 8px;
    font-size: 11px;
}}

/* Scrollbars */
QScrollBar:vertical {{
    background-color: transparent;
    width: 6px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background-color: {BORDER_SUBTLE};
    min-height: 20px;
    border-radius: 3px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {TEXT_MUTED};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

/* Status Bar */
QStatusBar {{
    background-color: {BG_SIDEBAR};
    border-top: 1px solid {BORDER_SUBTLE};
    color: {TEXT_MUTED};
    font-size: 11px;
    padding: 3px 8px;
}}

/* Tab & Navigation Buttons */
QPushButton.nav-btn {{
    text-align: left;
    padding: 8px 12px;
    background-color: transparent;
    border: none;
    border-radius: 6px;
    color: {TEXT_MUTED};
    font-weight: 500;
    font-size: 13px;
}}

QPushButton.nav-btn:hover {{
    background-color: {BG_HOVER};
    color: {TEXT_PRIMARY};
}}

QPushButton.nav-btn:checked {{
    background-color: #2F2147;
    color: {ACCENT_LAVENDER};
    font-weight: 700;
    border-left: 3px solid {ACCENT_PURPLE};
}}
"""
