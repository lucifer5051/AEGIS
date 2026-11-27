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


def build_whatsapp_parser() -> argparse.ArgumentParser:
    """Build parser for SAGE WhatsApp administrative CLI."""
    parser = argparse.ArgumentParser(
        prog="sage whatsapp",
        description="SAGE WhatsApp Whitelist & Contact Management",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override runtime SQLite DB path.",
    )
    subparsers = parser.add_subparsers(dest="whatsapp_subcommand")

    # run [--simulate] [--auth-dir PATH] [--scenario PATH] [--db-path PATH] [--model NAME] [--ollama-host URL]
    run_parser = subparsers.add_parser("run", help="Run the WhatsApp bridge supervisor or offline simulation.")
    run_parser.add_argument("--simulate", action="store_true", help="Run offline scripted 5-turn demo scenario.")
    run_parser.add_argument("--auth-dir", default=None, help="Directory for Baileys auth credentials.")
    run_parser.add_argument("--scenario", default=None, help="Path to scenario JSON file for --simulate.")
    run_parser.add_argument("--owner-jid", default=None, help="Owner phone number JID (<digits>@s.whatsapp.net).")
    run_parser.add_argument("--model", default=None, help="Local model name (default: qwen3.5:4b).")
    run_parser.add_argument("--ollama-host", default="http://127.0.0.1:11434", help="Local Ollama daemon URL (default: http://127.0.0.1:11434).")

    # status
    subparsers.add_parser("status", help="Show WhatsApp gateway and bridge operational status.")

    # unlink [--yes] [--auth-dir PATH]
    unlink_parser = subparsers.add_parser("unlink", help="Unlink WhatsApp session and remove credentials.")
    unlink_parser.add_argument("--yes", action="store_true", help="Confirm unlink without interactive prompt.")
    unlink_parser.add_argument("--auth-dir", default=None, help="Directory for Baileys auth credentials.")

    # smoke-test [--fake] [--model NAME] [--ollama-host URL]
    smoke_parser = subparsers.add_parser("smoke-test", help="Run synthetic smoke test on drafting pipeline.")
    smoke_parser.add_argument("--fake", action="store_true", help="Use scripted fake model for offline/CI testing.")
    smoke_parser.add_argument("--model", default=None, help="Local model name (default: qwen3.5:4b).")
    smoke_parser.add_argument("--model-alias", default=None, help="Model alias to test (legacy alias for --model).")
    smoke_parser.add_argument("--ollama-host", default="http://127.0.0.1:11434", help="Local Ollama daemon URL (default: http://127.0.0.1:11434).")

    contacts_parser = subparsers.add_parser("contacts", help="Manage whitelisted contacts.")
    contacts_sub = contacts_parser.add_subparsers(dest="contacts_action")

    # list
    contacts_sub.add_parser("list", help="List all whitelisted WhatsApp contacts.")

    # add <number> [--label NAME] [--mode {normal,banter}]
    add_parser = contacts_sub.add_parser("add", help="Add a contact to the whitelist.")
    add_parser.add_argument("number", help="Phone number or canonical JID of the contact.")
    add_parser.add_argument("--label", default=None, help="Display label/name for the contact.")
    add_parser.add_argument(
        "--mode",
        choices=["normal", "banter"],
        default="normal",
        help="Drafting style mode (default: normal).",
    )

    # remove <number>
    remove_parser = contacts_sub.add_parser("remove", help="Remove a contact from the whitelist.")
    remove_parser.add_argument("number", help="Phone number or canonical JID of the contact.")

    # mode <number> <normal|banter>
    mode_parser = contacts_sub.add_parser("mode", help="Change drafting style mode for a contact.")
    mode_parser.add_argument("number", help="Phone number or canonical JID of the contact.")
    mode_parser.add_argument("style_mode", choices=["normal", "banter"], help="Drafting style mode.")

    return parser


def normalize_cli_contact_input(raw: str) -> str:
    """Normalize user CLI contact input (e.g. '+91 98765 43210' or '1234567890@s.whatsapp.net')."""
    from src.security.contact_key_store import normalize_and_validate_contact_jid

    cleaned = (raw or "").strip()
    if "@" in cleaned:
        return normalize_and_validate_contact_jid(cleaned)
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    return normalize_and_validate_contact_jid(f"{digits}@s.whatsapp.net")


def handle_whatsapp_cli(argv: list[str]) -> int:
    """Handle SAGE WhatsApp contact CLI invocations."""
    parser = build_whatsapp_parser()
    args = parser.parse_args(argv)

    if not args.whatsapp_subcommand:
        parser.print_help()
        return 0

    from datetime import datetime, timezone
    from src.security.contact_key_store import ContactKeyStore
    from src.security.dpapi import WindowsDPAPIKeyProtector
    from src.storage.database import initialize_runtime_database
    from src.gateway.drafts import DraftManager
    from src.gateway.baileys_sidecar import generate_allowlist_file
    from src.config.models import MAX_WHATSAPP_CONTACTS

    db_path = Path(args.db_path) if args.db_path else _get_runtime_db_path()
    migrations_dir = _get_migrations_dir()

    if args.whatsapp_subcommand == "status":
        from src.gateway.whatsapp_runtime import get_whatsapp_status, print_whatsapp_status
        auth_dir = Path.home() / ".aegis" / "channels" / "whatsapp" / "session"
        try:
            conn = initialize_runtime_database(db_path, migrations_dir=migrations_dir)
            status_data = get_whatsapp_status(conn, auth_dir=auth_dir)
            print_whatsapp_status(status_data)
            conn.close()
            return 0
        except Exception as exc:
            print(f"[ERROR] Could not read WhatsApp status: {exc}", file=sys.stderr)
            return 1

    elif args.whatsapp_subcommand == "unlink":
        from src.gateway.whatsapp_runtime import unlink_whatsapp_session
        auth_dir = Path(args.auth_dir) if args.auth_dir else (Path.home() / ".aegis" / "channels" / "whatsapp" / "session")
        return unlink_whatsapp_session(auth_dir=auth_dir, force_yes=args.yes)

    elif args.whatsapp_subcommand == "run":
        from src.gateway.whatsapp_runtime import run_live_whatsapp, run_simulated_whatsapp
        if args.simulate:
            scenario_path = Path(args.scenario) if args.scenario else None
            return run_simulated_whatsapp(scenario_path=scenario_path, db_path=db_path if args.db_path else None)
        else:
            auth_dir = Path(args.auth_dir) if args.auth_dir else (Path.home() / ".aegis" / "channels" / "whatsapp" / "session")
            bridge_dir = Path(__file__).parent.parent / "bridge"
            return run_live_whatsapp(
                db_path=db_path,
                auth_dir=auth_dir,
                bridge_dir=bridge_dir,
                migrations_dir=migrations_dir,
                owner_jid=args.owner_jid,
                model_alias=args.model,
                ollama_host=args.ollama_host,
            )

    elif args.whatsapp_subcommand == "smoke-test":
        from src.gateway.whatsapp_runtime import run_smoke_test
        model_name = args.model or args.model_alias or "qwen3.5:4b"
        return run_smoke_test(
            use_fake=args.fake,
            model_alias=model_name,
            ollama_host=args.ollama_host,
        )

    if args.whatsapp_subcommand != "contacts" or not args.contacts_action:
        parser.print_help()
        return 0
    conn = initialize_runtime_database(db_path, migrations_dir=migrations_dir)
    key_store = ContactKeyStore(conn, WindowsDPAPIKeyProtector())
    draft_manager = DraftManager(conn, key_store)

    action = args.contacts_action

    if action == "list":
        cur = conn.execute(
            "SELECT contact_jid, label, style_mode, updated_at FROM whatsapp_contact_settings ORDER BY contact_jid"
        )
        rows = cur.fetchall()
        if not rows:
            print("No whitelisted WhatsApp contacts found.")
            return 0
        print(f"Whitelisted WhatsApp Contacts ({len(rows)}/{MAX_WHATSAPP_CONTACTS}):")
        for idx, (c_jid, label, style_mode, up_at) in enumerate(rows, 1):
            lbl = f" ({label})" if label else ""
            print(f"  {idx}. {c_jid}{lbl} - mode: {style_mode}")
        return 0

    elif action == "add":
        try:
            canonical_jid = normalize_cli_contact_input(args.number)
        except Exception as exc:
            print(f"[ERROR] Invalid contact number/JID: {exc}", file=sys.stderr)
            return 1

        # Check contact cap
        cur = conn.execute(
            "SELECT COUNT(*) FROM whatsapp_contact_settings WHERE contact_jid != ?",
            (canonical_jid,),
        )
        count = cur.fetchone()[0]
        if count >= MAX_WHATSAPP_CONTACTS:
            print(f"[ERROR] Cannot add contact: whitelist limit of {MAX_WHATSAPP_CONTACTS} contacts reached.", file=sys.stderr)
            return 1

        now_iso = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute(
                """
                INSERT INTO whatsapp_contact_settings (
                    contact_jid, label, style_mode, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(contact_jid) DO UPDATE SET
                    label = COALESCE(excluded.label, whatsapp_contact_settings.label),
                    style_mode = excluded.style_mode,
                    updated_at = excluded.updated_at
                """,
                (canonical_jid, args.label, args.mode, now_iso),
            )

        # Regenerate allowlist
        cur = conn.execute("SELECT contact_jid FROM whatsapp_contact_settings ORDER BY contact_jid")
        all_jids = [r[0] for r in cur.fetchall()]
        try:
            generate_allowlist_file(all_jids)
        except Exception as exc:
            logger.warning("Could not regenerate allowlist file: %s", exc)

        print(f"Added {canonical_jid} to whitelist (mode: {args.mode}). Whitelist edits take effect on gateway restart.")
        conn.close()
        return 0

    elif action == "remove":
        try:
            canonical_jid = normalize_cli_contact_input(args.number)
        except Exception as exc:
            print(f"[ERROR] Invalid contact number/JID: {exc}", file=sys.stderr)
            conn.close()
            return 1

        with conn:
            conn.execute(
                "DELETE FROM whatsapp_contact_settings WHERE contact_jid = ?",
                (canonical_jid,),
            )

        cancelled_count = draft_manager.cancel_drafts_for_contact(canonical_jid)

        cur = conn.execute("SELECT contact_jid FROM whatsapp_contact_settings ORDER BY contact_jid")
        all_jids = [r[0] for r in cur.fetchall()]
        try:
            generate_allowlist_file(all_jids)
        except Exception as exc:
            logger.warning("Could not regenerate allowlist file: %s", exc)

        print(f"Removed {canonical_jid} from whitelist. Cancelled {cancelled_count} pending draft(s). Whitelist edits take effect on gateway restart.")
        conn.close()
        return 0

    elif action == "mode":
        try:
            canonical_jid = normalize_cli_contact_input(args.number)
        except Exception as exc:
            print(f"[ERROR] Invalid contact number/JID: {exc}", file=sys.stderr)
            conn.close()
            return 1

        cur = conn.execute("SELECT contact_jid FROM whatsapp_contact_settings WHERE contact_jid = ?", (canonical_jid,))
        if not cur.fetchone():
            print(f"[ERROR] Contact {canonical_jid} is not in the whitelist.", file=sys.stderr)
            conn.close()
            return 1

        now_iso = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute(
                "UPDATE whatsapp_contact_settings SET style_mode = ?, updated_at = ? WHERE contact_jid = ?",
                (args.style_mode, now_iso, canonical_jid),
            )

        print(f"Updated drafting mode for {canonical_jid} to '{args.style_mode}'. Whitelist edits take effect on gateway restart.")
        conn.close()
        return 0

    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI main entry point. Returns exit code."""
    raw_args = list(sys.argv[1:]) if argv is None else list(argv)
    if raw_args and raw_args[0] == "whatsapp":
        return handle_whatsapp_cli(raw_args[1:])

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
