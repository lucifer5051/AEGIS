"""SAGE CLI Entry Point.

Usage:
    python -m src "create a Python file that prints hello world"
    python -m src --help

Invariants:
- Zero network calls.
- No secrets in output or logs.
- All file operations remain within allowed local roots.
- DB initialized under %USERPROFILE%\\.aegis\runtime\\sage.db.
- Logs under %USERPROFILE%\\.aegis\\logs\\sage.log.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_runtime_db_path() -> Path:
    return Path.home() / ".aegis" / "runtime" / "sage.db"


def _get_migrations_dir() -> Path:
    """Resolve migrations dir relative to this file (src/cli.py -> project root/migrations)."""
    return Path(__file__).parent.parent / "migrations"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sage",
        description="SAGE — Secure Autonomous Goal Executor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  sage "create a Python file that prints hello world"\n'
            '  sage --autonomy-tier 2 "list files in the current directory"\n'
        ),
    )
    parser.add_argument(
        "goal",
        nargs="?",
        help="Natural language goal for SAGE to execute.",
    )
    parser.add_argument(
        "--autonomy-tier",
        type=int,
        default=0,
        choices=[0, 1, 2, 3],
        help=("Autonomy tier (0=STRICT_MANUAL [default], 1=SUPERVISED, 2=SEMI_AUTO, 3=FULL_AUTO)."),
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Optional project scope identifier.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "Skip loading the local model (planning will fail for real goals; useful for testing)."
        ),
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override runtime SQLite DB path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI main entry point. Returns exit code."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if not args.goal:
        parser.print_help()
        return 0

    # === Setup Logging ===
    try:
        from src.config.logging import setup_sage_logging

        setup_sage_logging()
    except Exception as exc:
        logging.basicConfig(level=logging.WARNING)
        logging.warning("Could not configure SAGE logging: %s", exc)

    logger.info("SAGE CLI starting for goal: %r", args.goal)

    # === Initialize Central Application Runtime ===
    db_path = Path(args.db_path) if args.db_path else _get_runtime_db_path()
    migrations_dir = _get_migrations_dir()

    tier_map = {
        0: "STRICT_MANUAL",
        1: "SAFE_BOUNDED",
        2: "SUPERVISED",
        3: "FULL_BOUNDED",
    }
    tier_name = tier_map.get(args.autonomy_tier, "STRICT_MANUAL")

    try:
        from src.runtime import SageApplication

        app = SageApplication(
            db_path=db_path,
            migrations_dir=migrations_dir,
        )
        # Update autonomy profile tier if explicitly requested
        app.config_manager.update_config(
            updates={"autonomy_profile": {"tier": tier_name}},
            actor="cli_user",
        )
        app.start()
    except Exception as exc:
        print(f"[SAGE ERROR] Failed to initialize SAGE runtime: {exc}", file=sys.stderr)
        logger.exception("Runtime initialization failed")
        return 1

    try:
        # === Execute Goal ===
        result = app.supervisor.execute_goal(
            goal_text=args.goal,
            actor="cli_user",
            project_id=args.project_id,
        )
    except Exception as exc:
        print(f"[SAGE ERROR] Execution failed unexpectedly: {exc}", file=sys.stderr)
        logger.exception("execute_goal raised an unhandled exception")
        app.stop()
        return 1

    # === Output Result ===
    status_label = "✓ COMPLETED" if result.success else "✗ FAILED"
    print(f"\n{status_label}\n")
    print(result.synthesis)

    if result.task_record:
        print(f"\n[Task ID: {result.task_record.task_id}]")

    app.stop()
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
