"""SAGE UI Launch Script.

Usage:
    python sage_ui.py [--port PORT] [--token TOKEN] [--host HOST]

Starts SageApplication and the FastAPI UI server on localhost.
The session token is printed on startup and embedded in the browser URL.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
import webbrowser
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sage_ui")


def _find_available_port(host: str, preferred_port: int) -> int:
    import socket
    candidates = [preferred_port, 9876, 8888, 8088, 9090]
    for port in candidates:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
                return port
        except OSError:
            continue
    return preferred_port


def main() -> None:
    parser = argparse.ArgumentParser(description="SAGE Live Dashboard")
    parser.add_argument("--port", type=int, default=8765, help="UI server port (default 8765)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1 — localhost only)")
    parser.add_argument("--token", default=None, help="Session token (auto-generated if not provided)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    parser.add_argument("--workers", type=int, default=0, help="Background task workers to start (default 0)")
    args = parser.parse_args()

    port = _find_available_port(args.host, args.port)
    if port != args.port:
        logger.warning("Port %d is in use; using port %d instead.", args.port, port)

    token = args.token or secrets.token_urlsafe(24)
    url = f"http://{args.host}:{port}/?token={token}"

    logger.info("=" * 60)
    logger.info("  SAGE Live Dashboard")
    logger.info("  URL   : %s", url)
    logger.info("  Token : %s", token)
    logger.info("=" * 60)

    # Build SageApplication
    logger.info("Initializing SAGE Application…")
    from src.runtime import SageApplication

    sage_app = SageApplication(start_workers=args.workers > 0, num_workers=max(args.workers, 1))
    sage_app.start()
    logger.info("SAGE Application started.")

    # Attach UI event bridge
    from src.ui.bridge import attach as attach_bridge
    attach_bridge(sage_app)

    # Build FastAPI app
    from src.ui.server import create_app
    fastapi_app = create_app(sage_app, session_token=token)

    # Open browser
    if not args.no_browser:
        import threading
        def _open():
            import time; time.sleep(1.5)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    # Serve
    import uvicorn
    try:
        uvicorn.run(fastapi_app, host=args.host, port=port, log_level="warning")
    finally:
        logger.info("Shutting down SAGE…")
        sage_app.stop()
        logger.info("Done.")


if __name__ == "__main__":
    main()
