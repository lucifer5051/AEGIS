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

    # === Initialize Database ===
    db_path = Path(args.db_path) if args.db_path else _get_runtime_db_path()
    migrations_dir = _get_migrations_dir()

    try:
        from src.storage.database import initialize_runtime_database

        conn = initialize_runtime_database(db_path, migrations_dir=migrations_dir)
    except Exception as exc:
        print(f"[SAGE ERROR] Failed to initialize database: {exc}", file=sys.stderr)
        logger.exception("Database initialization failed")
        return 1

    # === Build Trust Authority ===
    try:
        from src.trust import build_trust_authority
        from src.trust.policy import AutonomyTier

        tier_map = {
            0: AutonomyTier.STRICT_MANUAL,
            1: AutonomyTier.SAFE_BOUNDED,
            2: AutonomyTier.SUPERVISED,
            3: AutonomyTier.FULL_BOUNDED,
        }
        tier = tier_map.get(args.autonomy_tier, AutonomyTier.STRICT_MANUAL)
        trust_authority = build_trust_authority(conn, autonomy_tier=tier)
    except Exception as exc:
        print(f"[SAGE ERROR] Failed to build Trust Authority: {exc}", file=sys.stderr)
        return 1

    # === Build Tool Broker with Builtins ===
    try:
        from src.tools.broker import ToolBroker
        from src.tools.builtin import register_builtin_tools

        broker = ToolBroker(trust_authority)
        register_builtin_tools(broker)
    except Exception as exc:
        print(f"[SAGE ERROR] Failed to register tools: {exc}", file=sys.stderr)
        return 1

    # === Load Reasoning Provider (optional) ===
    reasoning_provider = None
    if not args.no_llm:
        try:
            from src.reasoning.provider import LlamaCppReasoningProvider

            reasoning_provider = LlamaCppReasoningProvider()
            reasoning_provider.initialize()
            logger.info("Local reasoning provider initialized successfully.")
        except Exception as exc:
            print(
                f"[SAGE WARNING] Local reasoning provider not available: {exc}\n"
                "Run with --no-llm to suppress this warning.",
                file=sys.stderr,
            )
            reasoning_provider = None

    # === Build State Store & Supervisor ===
    try:
        from src.manager.state_store import StateStore
        from src.manager.supervisor import Supervisor

        state_store = StateStore(conn)
        supervisor = Supervisor(
            state_store=state_store,
            trust_authority=trust_authority,
            tool_broker=broker,
            reasoning_provider=reasoning_provider,
        )
    except Exception as exc:
        print(f"[SAGE ERROR] Failed to build Supervisor: {exc}", file=sys.stderr)
        return 1

    # === Execute Goal ===
    if reasoning_provider is None:
        print(
            "[SAGE] No reasoning provider loaded. Cannot plan goal.\n"
            "Ensure the local model is present or check the logs.",
            file=sys.stderr,
        )
        return 1

    try:
        result = supervisor.execute_goal(
            goal_text=args.goal,
            actor="cli_user",
            project_id=args.project_id,
        )
    except Exception as exc:
        print(f"[SAGE ERROR] Execution failed unexpectedly: {exc}", file=sys.stderr)
        logger.exception("execute_goal raised an unhandled exception")
        return 1

    # === Output Result ===
    status_label = "✓ COMPLETED" if result.success else "✗ FAILED"
    print(f"\n{status_label}\n")
    print(result.synthesis)

    if result.task_record:
        print(f"\n[Task ID: {result.task_record.task_id}]")

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
