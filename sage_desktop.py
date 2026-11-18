"""sage_desktop.py — Standalone PySide6 launcher for the SAGE desktop application.

Usage:
    C:\\Users\\digvi\\.aegis\\venv\\Scripts\\python.exe sage_desktop.py

No browser required.  The window opens natively on the Windows desktop.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Project root on path before any src imports
sys.path.insert(0, str(Path(__file__).parent))

# Allow Qt to find its platform plugin on Windows without a display server
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sage_desktop")


def main() -> None:
    from PySide6.QtWidgets import QApplication

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("SAGE")
    qt_app.setOrganizationName("AEIGS")

    logger.info("Initialising SAGE Application…")
    from src.runtime import SageApplication

    sage_app = SageApplication(start_workers=False)
    sage_app.start()
    logger.info("SAGE Application started.")

    # Attach EventBus bridge so the FastAPI server (if running) also gets events
    try:
        from src.ui.bridge import attach as attach_bridge
        attach_bridge(sage_app)
    except Exception as exc:
        logger.debug("UI bridge attach skipped: %s", exc)

    from src.ui.desktop.main_window import SageMainWindow

    window = SageMainWindow(sage_app)
    window.show()

    exit_code = qt_app.exec()
    logger.info("SAGE Desktop exited (code %d).", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
