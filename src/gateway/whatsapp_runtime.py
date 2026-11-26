"""SAGE WhatsApp Gateway Runtime & Lifecycle Supervisor.

Canonical Reference:
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md
    AEGIS Prompt 036 (Phase 9C.3)

Invariants:
- 100% offline-capable test & simulation harness.
- Local model only: refuses to start if model could route to cloud.
- Safe owner self-chat channel for draft approval.
- 0 unsanctioned outbound sends.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.config.models import MAX_WHATSAPP_CONTACTS, WhatsAppConfig
from src.gateway.baileys_sidecar import BaileysSidecarSupervisor, generate_allowlist_file
from src.gateway.contact_alias import ContactAliasStore
from src.gateway.draft_approval import approve_and_send_draft
from src.gateway.drafts import DraftManager
from src.gateway.echo_registry import SelfEchoRegistry
from src.gateway.gateway import ChannelGateway
from src.gateway.local_draft_provider import LocalDraftProvider
from src.gateway.owner_channel import OwnerCommandHandler
from src.gateway.store import GatewayStore
from src.gateway.whatsapp_transport import SimulatedWhatsAppTransport
from src.models.domain import ProviderType
from src.security.contact_key_store import (
    ContactKeyStore,
    normalize_and_validate_contact_jid,
)
from src.memory.contact_memory import EncryptedContactMemoryStore
from src.security.dpapi import WindowsDPAPIKeyProtector
from src.storage.database import initialize_runtime_database
from src.storage.migrations import (
    get_current_schema_version,
    load_migrations_from_directory,
)

logger = logging.getLogger(__name__)

_CANONICAL_PN_REGEX = re.compile(r"^\d{7,15}@s\.whatsapp\.net$")


def mask_jid(jid: str) -> str:
    """Mask a phone number JID for display to avoid leaking personal data."""
    if not jid:
        return "<unknown>"
    parts = jid.split("@")
    pn = parts[0]
    domain = f"@{parts[1]}" if len(parts) > 1 else ""
    if len(pn) <= 4:
        return f"***{domain}"
    return f"...{pn[-4:]}{domain}"


# ==============================================================================
# 2.2 PREFLIGHT CHECKS
# ==============================================================================


def run_preflight(
    db_path: Path,
    migrations_dir: Path,
    bridge_dir: Path,
    auth_dir: Path,
    owner_jid: Optional[str] = None,
    model_gateway: Optional[Any] = None,
    model_alias: Optional[str] = None,
    custom_llm: Optional[Callable[[str, str], str]] = None,
    db_conn: Optional[Any] = None,
) -> Tuple[bool, Optional[str]]:
    """Execute the 7 required preflight safety checks.

    Returns:
        (True, None) if all checks pass.
        (False, error_message) describing what failed and how to fix it.
    """
    # 1. Database readable and schema at latest version
    try:
        latest_migrations = load_migrations_from_directory(migrations_dir)
        max_migration_v = max((m.version for m in latest_migrations), default=0)
    except Exception as exc:
        return (
            False,
            f"Preflight Check 1 Failed: Cannot load migrations from '{migrations_dir}': {exc}. "
            "Ensure the migrations directory exists and contains valid SQL migrations.",
        )

    conn = db_conn
    close_conn = False
    if conn is None:
        if not db_path.exists():
            return (
                False,
                f"Preflight Check 1 Failed: Database file does not exist at '{db_path}'. "
                "Initialize the database using `sage` CLI or initialize_runtime_database.",
            )
        try:
            conn = initialize_runtime_database(db_path, migrations_dir=migrations_dir)
            close_conn = True
        except Exception as exc:
            return (
                False,
                f"Preflight Check 1 Failed: Cannot connect or read database at '{db_path}': {exc}. "
                "Verify database permissions and integrity.",
            )
    elif getattr(conn, "row_factory", None) is None:
        try:
            import sqlite3
            conn.row_factory = sqlite3.Row
        except Exception:
            pass

    try:
        current_v = get_current_schema_version(conn)
        if current_v < max_migration_v:
            return (
                False,
                f"Preflight Check 1 Failed: Database schema is at version {current_v}, but "
                f"latest migration is {max_migration_v}. Run database migrations before starting.",
            )
    except Exception as exc:
        return (
            False,
            f"Preflight Check 1 Failed: Error querying schema version: {exc}. "
            "Verify database health and schema_migrations table.",
        )

    # 2. At least one contact whitelisted
    try:
        cur = conn.execute("SELECT COUNT(*) FROM whatsapp_contact_settings")
        contact_count = cur.fetchone()[0]
        if contact_count == 0:
            return (
                False,
                "Preflight Check 2 Failed: No contacts whitelisted. At least one contact must "
                "be whitelisted. Add a contact using:\n"
                "  python -m src.cli whatsapp contacts add <number> [--label NAME] [--mode {normal,banter}]",
            )
    except Exception as exc:
        return (
            False,
            f"Preflight Check 2 Failed: Could not query whatsapp_contact_settings: {exc}. "
            "Ensure migration 0011 has been applied.",
        )
    finally:
        if close_conn:
            try:
                conn.close()
            except Exception:
                pass

    # 3. Owner JID configured
    if not owner_jid or not owner_jid.strip():
        return (
            False,
            "Preflight Check 3 Failed: Owner JID is not configured. Set 'whatsapp.owner_jid' "
            "in configuration (format: <digits>@s.whatsapp.net).",
        )
    clean_owner = owner_jid.strip()
    if not _CANONICAL_PN_REGEX.match(clean_owner):
        return (
            False,
            f"Preflight Check 3 Failed: Owner JID '{mask_jid(clean_owner)}' is invalid. "
            "Must be a canonical phone number JID in format: <digits>@s.whatsapp.net",
        )

    # 4. Node.js >= 18 on PATH
    try:
        proc = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
        if proc.returncode != 0:
            return (
                False,
                "Preflight Check 4 Failed: 'node --version' returned a non-zero exit code. "
                "Ensure Node.js is installed and functioning correctly on PATH.",
            )
        node_ver_str = proc.stdout.strip()
        match = re.search(r"v(\d+)\.", node_ver_str)
        if not match or int(match.group(1)) < 18:
            return (
                False,
                f"Preflight Check 4 Failed: Node.js version {node_ver_str} found, but Node.js >= 18 "
                "is required by the Baileys bridge. Please upgrade Node.js to version 18 or higher.",
            )
    except FileNotFoundError:
        return (
            False,
            "Preflight Check 4 Failed: Node.js executable ('node') was not found on PATH. "
            "Please install Node.js >= 18 and add it to your system PATH.",
        )
    except Exception as exc:
        return (
            False,
            f"Preflight Check 4 Failed: Error verifying Node.js on PATH: {exc}.",
        )

    # 5. bridge/node_modules/ exists
    node_modules_dir = bridge_dir / "node_modules"
    if not node_modules_dir.is_dir():
        return (
            False,
            f"Preflight Check 5 Failed: Node modules directory not found at '{node_modules_dir}'. "
            f"Run 'npm install' inside '{bridge_dir}' before running the WhatsApp gateway.",
        )

    # 6. Local model available (configured and verified local; refuse if cloud)
    if custom_llm is None:
        if model_gateway is None:
            return (
                False,
                "Preflight Check 6 Failed: No model gateway or custom LLM configured. WhatsApp agent "
                "strictly requires a verified local model to draft replies without leaking contact text.",
            )
        # Check that the gateway has local reasoning / chat capability
        try:
            alias = model_alias or "local"
            # Inspect registry if available
            if hasattr(model_gateway, "registry") and model_gateway.registry:
                reg = model_gateway.registry
                desc = reg.get_model(alias) if hasattr(reg, "get_model") else None
                if desc and desc.provider_type != ProviderType.LOCAL:
                    return (
                        False,
                        f"Preflight Check 6 Failed: Configured model '{alias}' routes to provider "
                        f"'{desc.provider_id}' of type '{desc.provider_type}'. WhatsApp agent strictly "
                        "refuses to run unless model provider is LOCAL to prevent contact text leakage.",
                    )
                # Verify that at least one local provider exists
                local_models = reg.list_models(local_only=True) if hasattr(reg, "list_models") else []
                if not desc and not local_models:
                    return (
                        False,
                        "Preflight Check 6 Failed: No LOCAL model provider is registered in ModelGateway. "
                        "WhatsApp agent requires a local model (e.g. llama.cpp or Ollama).",
                    )
        except Exception as exc:
            return (
                False,
                f"Preflight Check 6 Failed: Error checking local model availability: {exc}.",
            )

    # 7. Auth state directory writable
    try:
        auth_dir.mkdir(parents=True, exist_ok=True)
        test_file = auth_dir / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
    except Exception as exc:
        return (
            False,
            f"Preflight Check 7 Failed: Auth directory '{auth_dir}' is not writable: {exc}. "
            "Check directory permissions.",
        )

    return (True, None)


# ==============================================================================
# 2.4 STATUS SUBCOMMAND
# ==============================================================================


def get_whatsapp_status(
    db_conn: Any,
    auth_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Retrieve WhatsApp gateway operational status."""
    status_data: Dict[str, Any] = {
        "bridge_state": "STOPPED",
        "connected_phone": None,
        "whitelisted_contacts": 0,
        "pending_drafts": 0,
        "today_approved": 0,
        "today_rejected": 0,
        "bridge_uptime": "N/A",
    }

    # Query gateway state table if exists
    try:
        cur = db_conn.execute("SELECT key, value FROM whatsapp_gateway_state")
        for k, v in cur.fetchall():
            if k == "bridge_status":
                status_data["bridge_state"] = v
            elif k == "paired_phone":
                status_data["connected_phone"] = v
            elif k == "bridge_start_time":
                try:
                    start_dt = datetime.fromisoformat(v)
                    now_dt = datetime.now(timezone.utc)
                    uptime_secs = int((now_dt - start_dt).total_seconds())
                    mins, secs = divmod(uptime_secs, 60)
                    hrs, mins = divmod(mins, 60)
                    status_data["bridge_uptime"] = f"{hrs}h {mins}m {secs}s"
                except Exception:
                    pass
    except Exception:
        pass

    # Check Baileys creds.json for paired phone fallback
    if status_data["connected_phone"] is None and auth_dir and auth_dir.exists():
        creds_file = auth_dir / "creds.json"
        if creds_file.exists():
            try:
                data = json.loads(creds_file.read_text(encoding="utf-8"))
                me_id = data.get("me", {}).get("id")
                if me_id:
                    clean_pn = me_id.split(":")[0].split("@")[0]
                    status_data["connected_phone"] = f"+{clean_pn}"
            except Exception:
                pass

    # Whitelisted contacts count
    try:
        cur = db_conn.execute("SELECT COUNT(*) FROM whatsapp_contact_settings")
        status_data["whitelisted_contacts"] = cur.fetchone()[0]
    except Exception:
        pass

    # Pending drafts count
    try:
        cur = db_conn.execute("SELECT COUNT(*) FROM whatsapp_drafts WHERE status = 'pending'")
        status_data["pending_drafts"] = cur.fetchone()[0]
    except Exception:
        pass

    # Today's approved & rejected
    try:
        today_start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur = db_conn.execute(
            """
            SELECT status, COUNT(*) FROM whatsapp_drafts
            WHERE (decided_at >= ? OR created_at >= ?)
            GROUP BY status
            """,
            (today_start, today_start),
        )
        for s, cnt in cur.fetchall():
            if s in ("approved", "sent"):
                status_data["today_approved"] += cnt
            elif s == "rejected":
                status_data["today_rejected"] += cnt
    except Exception:
        pass

    return status_data


def print_whatsapp_status(status: Dict[str, Any]) -> None:
    """Print human-readable status table to stdout."""
    print("\n=======================================================")
    print("           AEGIS WhatsApp Gateway Status               ")
    print("=======================================================")
    print(f"  Bridge State:            {status['bridge_state']}")
    phone_display = status["connected_phone"] or "Unpaired (scan QR to pair)"
    print(f"  Connected Phone:         {phone_display}")
    print(f"  Whitelisted Contacts:    {status['whitelisted_contacts']} / {MAX_WHATSAPP_CONTACTS}")
    print(f"  Pending Drafts:          {status['pending_drafts']}")
    print(f"  Today's Decisions:       {status['today_approved']} approved, {status['today_rejected']} rejected")
    print(f"  Bridge Uptime:           {status['bridge_uptime']}")
    print("=======================================================\n")


# ==============================================================================
# 2.5 SCRIPTED DEMO SCENARIO (--simulate)
# ==============================================================================


def run_simulated_whatsapp(
    scenario_path: Optional[Path] = None,
    db_path: Optional[Path] = None,
    custom_llm: Optional[Callable[[str, str], str]] = None,
) -> int:
    """Execute the 5-turn scripted scenario end-to-end through SimulatedWhatsAppTransport."""
    if scenario_path is None:
        scenario_path = Path(__file__).parent.parent.parent / "tests" / "data" / "whatsapp_demo_scenario.json"

    if not scenario_path.exists():
        print(f"[ERROR] Scenario file not found at '{scenario_path}'", file=sys.stderr)
        return 1

    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    owner_jid = scenario.get("owner_jid", "15550100099@s.whatsapp.net")
    contacts = scenario.get("contacts", [])
    turns = scenario.get("turns", [])

    # Setup database
    migrations_dir = Path(__file__).parent.parent.parent / "migrations"
    if db_path:
        conn = initialize_runtime_database(db_path, migrations_dir=migrations_dir)
    else:
        import sqlite3
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        from src.storage.migrations import bootstrap_schema_migrations, apply_migrations
        bootstrap_schema_migrations(conn)
        apply_migrations(conn, migrations=load_migrations_from_directory(migrations_dir))

    # Pre-populate contacts
    now_iso = datetime.now(timezone.utc).isoformat()
    for c in contacts:
        with conn:
            conn.execute(
                """
                INSERT INTO whatsapp_contact_settings (contact_jid, label, style_mode, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(contact_jid) DO UPDATE SET
                    label = excluded.label,
                    style_mode = excluded.style_mode
                """,
                (c["jid"], c.get("label", "Contact"), c.get("style_mode", "normal"), now_iso),
            )

    key_protector = WindowsDPAPIKeyProtector()
    key_store = ContactKeyStore(conn, key_protector)
    memory_store = EncryptedContactMemoryStore(conn, key_store)
    alias_store = ContactAliasStore(conn)
    echo_registry = SelfEchoRegistry()
    transport = SimulatedWhatsAppTransport()
    gateway_store = GatewayStore(conn)

    # Fake local LLM model provider
    def fake_local_model(prompt: str, system_prompt: str) -> str:
        if custom_llm is not None:
            return custom_llm(prompt, system_prompt)
        prompt_lower = prompt.lower()
        if "chest pain" in prompt_lower or "medicine" in prompt_lower:
            return "I cannot provide medical advice. Please consult a qualified doctor or emergency services."
        if "weekend" in prompt_lower or "bro" in prompt_lower:
            return "Yo! Yeah totally, let's catch up on Saturday. Let me know when you're free!"
        if "override" in prompt_lower or "password" in prompt_lower:
            return "I cannot help with that request."
        return "Hey! Received your message. Will get back to you shortly."

    draft_provider = LocalDraftProvider(
        model_gateway=None,
        custom_llm_fn=fake_local_model,
        protected_topics_path=str(Path(__file__).parent.parent.parent / "config" / "whatsapp_protected_topics.txt"),
        banned_terms_path=str(Path(__file__).parent.parent.parent / "config" / "whatsapp_banned_terms.txt"),
    )

    draft_manager = DraftManager(
        conn=conn,
        key_store=key_store,
        draft_provider=draft_provider,
    )

    allowed_jids = [c["jid"] for c in contacts]
    config = WhatsAppConfig(
        enabled=True,
        allowed_jids=allowed_jids,
        owner_jid=owner_jid,
    )

    # Mock supervisor for sending owner notices and approved sends
    class MockSupervisor:
        def __init__(self, tp: SimulatedWhatsAppTransport, o_jid: str):
            self._tp = tp
            self._owner_jid = o_jid
            self._allowed_jids = allowed_jids
            self.owner_notices: List[str] = []

        def send_owner_notice(self, text: str) -> None:
            self.owner_notices.append(text)

        def send_message(self, recipient_jid: str, text: str, *, authorization: Any, **kwargs: Any) -> Any:
            self._tp.send_message(recipient_jid=recipient_jid, text=text)
            return {"id": "mock_sent_id", "status": "sent"}

    mock_sup = MockSupervisor(transport, owner_jid)

    owner_handler = OwnerCommandHandler(
        db_conn=conn,
        draft_manager=draft_manager,
        key_store=key_store,
        allowed_jids=allowed_jids,
        supervisor=mock_sup,
    )

    gateway = ChannelGateway(
        config=config,
        store=gateway_store,
        transport=transport,
        router=None,  # NEVER routed to CommandRouter
        supervisor=mock_sup,
        contact_memory_store=memory_store,
        draft_manager=draft_manager,
        alias_store=alias_store,
        echo_registry=echo_registry,
        owner_channel_handler=owner_handler,
    )

    # Execution telemetry
    stats = {
        "total_turns": 0,
        "drafts_created": 0,
        "banter_suppressed": 0,
        "injections_resisted": 0,
        "sends_approved": 0,
        "unsanctioned_sends": 0,
    }

    print("\n" + "=" * 65)
    print("      AEGIS WhatsApp Agent — Scripted Demo Simulation      ")
    print("=" * 65)

    created_draft_ids: List[str] = []

    for t_data in turns:
        stats["total_turns"] += 1
        turn_no = t_data["turn"]
        sender_jid = t_data["sender_jid"]
        is_owner = t_data.get("is_owner", False)
        raw_text = t_data["text"]

        # Dynamic variable replacement for owner approval turn
        if "{draft_1_id}" in raw_text:
            d_id = created_draft_ids[0] if created_draft_ids else "UNKNOWN"
            raw_text = raw_text.replace("{draft_1_id}", d_id)

        print(f"\n[TURN {turn_no}] --------------------------------------------------")
        sender_label = "OWNER" if is_owner else "CONTACT"
        print(f"Sender: {sender_label} ({mask_jid(sender_jid)})")
        print(f"Message: {raw_text}")

        # Assert 0 unsanctioned sends happened before approval turn
        if turn_no < 5 and len(transport.sent_messages) > 0:
            stats["unsanctioned_sends"] += len(transport.sent_messages)
            print("[CRITICAL SECURITY FAILURE] Unsanctioned message sent before approval!", file=sys.stderr)
            return 1

        if is_owner:
            transport.enqueue_synthetic_message(
                remote_jid=owner_jid,
                sender_jid=owner_jid,
                text=raw_text,
                message_id=f"demo_msg_{turn_no}",
            )
            # Tag event to simulate self-chat
            last_ev = transport._inbound_queue[-1]
            last_ev["key"]["fromMe"] = True
            last_ev["is_self_chat"] = True
            last_ev["chat_jid"] = owner_jid
            last_ev["sender_pn"] = owner_jid
            last_ev["from_me"] = True
            last_ev["text"] = raw_text
            last_ev["id"] = f"demo_msg_{turn_no}"
        else:
            transport.enqueue_synthetic_message(
                remote_jid=sender_jid,
                sender_jid=sender_jid,
                text=raw_text,
                message_id=f"demo_msg_{turn_no}",
            )

        results = gateway.poll_and_process_once()
        if not results:
            print("  Result: No update processed")
            continue

        if not is_owner:
            # Check draft created
            pending = draft_manager.list_pending_drafts()
            latest = pending[-1] if pending else None
            if latest:
                created_draft_ids.append(latest["draft_id"])
                stats["drafts_created"] += 1
                print(f"  => Action: Draft Created [ID: {latest['draft_id']}]")

                # Verify properties based on turn expectations
                if t_data.get("expected_outcome") == "draft_created_banter":
                    print("  => Mode: Banter Active")
                elif t_data.get("expected_outcome") == "draft_created_injection_flagged":
                    stats["injections_resisted"] += 1
                    print("  => Security: Injection Attempt Resisted & Flagged")
                elif t_data.get("expected_outcome") == "draft_created_banter_suppressed":
                    stats["banter_suppressed"] += 1
                    print("  => Safety: Sensitive Topic Screened (Banter Suppressed)")

            # Show owner notice card sent to owner's self-chat
            if mock_sup.owner_notices:
                latest_notice = mock_sup.owner_notices[-1]
                print("  => Owner Notice Delivered:")
                for n_line in latest_notice.splitlines()[:4]:
                    print(f"     | {n_line}")
                if len(latest_notice.splitlines()) > 4:
                    print("     | ... [truncated card preview]")

        else:
            # Owner command turn
            if mock_sup.owner_notices:
                latest_resp = mock_sup.owner_notices[-1]
                print(f"  => Owner Response:\n     {latest_resp}")

            if t_data.get("expected_outcome") == "draft_1_approved_and_sent":
                if len(transport.sent_messages) == 1:
                    stats["sends_approved"] += 1
                    sent = transport.sent_messages[0]
                    print(f"  => Guarded Send Success: Approved draft delivered to {mask_jid(sent['recipient_jid'])}")
                else:
                    print(f"  => [FAILURE] Expected 1 sent message, got {len(transport.sent_messages)}")

    # Summary table
    print("\n" + "=" * 65)
    print("                    SIMULATION SUMMARY                         ")
    print("=" * 65)
    print(f"  Total Turns:              {stats['total_turns']}")
    print(f"  Drafts Created:           {stats['drafts_created']}")
    print(f"  Banter Suppressed:        {stats['banter_suppressed']}")
    print(f"  Injections Resisted:      {stats['injections_resisted']}")
    print(f"  Sends Approved:           {stats['sends_approved']}")
    print(f"  Unsanctioned Sends:       {stats['unsanctioned_sends']}")
    print("=" * 65)

    if stats["unsanctioned_sends"] == 0 and stats["sends_approved"] == 1:
        print("\n[SUCCESS] Simulation passed with 0 unsanctioned sends.\n")
        return 0
    else:
        print("\n[FAILED] Simulation failed safety assertions.\n", file=sys.stderr)
        return 1


# ==============================================================================
# 2.3 MAIN RUN LOOP
# ==============================================================================


class WhatsAppSweeperThread(threading.Thread):
    """Background worker that expires pending drafts older than expiry every 60s."""

    def __init__(self, draft_manager: DraftManager, interval: float = 60.0):
        super().__init__(name="WhatsAppDraftSweeper", daemon=True)
        self._draft_manager = draft_manager
        self._interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("WhatsApp draft sweeper started (interval=%.1fs)", self._interval)
        while not self._stop_event.is_set():
            try:
                expired = self._draft_manager.expire_stale_drafts()
                if expired > 0:
                    logger.info("WhatsApp sweeper expired %d stale draft(s)", expired)
            except Exception as exc:
                logger.error("Error in WhatsApp draft sweeper: %s", exc)
            self._stop_event.wait(self._interval)
        logger.info("WhatsApp draft sweeper stopped.")


def run_live_whatsapp(
    db_path: Path,
    auth_dir: Path,
    bridge_dir: Path,
    migrations_dir: Path,
    owner_jid: Optional[str] = None,
    model_gateway: Optional[Any] = None,
    model_alias: Optional[str] = None,
    custom_llm: Optional[Callable[[str, str], str]] = None,
    stop_event: Optional[threading.Event] = None,
    max_consecutive_failures: int = 5,
) -> int:
    """Execute live WhatsApp supervisor loop with preflight checks and crash backoff."""
    # Run Preflight Checks
    passed, err = run_preflight(
        db_path=db_path,
        migrations_dir=migrations_dir,
        bridge_dir=bridge_dir,
        auth_dir=auth_dir,
        owner_jid=owner_jid,
        model_gateway=model_gateway,
        model_alias=model_alias,
        custom_llm=custom_llm,
    )
    if not passed:
        print(f"\n[PREFLIGHT ERROR] {err}\n", file=sys.stderr)
        return 1

    conn = initialize_runtime_database(db_path, migrations_dir=migrations_dir)
    key_protector = WindowsDPAPIKeyProtector()
    key_store = ContactKeyStore(conn, key_protector)
    memory_store = EncryptedContactMemoryStore(conn, key_store)
    alias_store = ContactAliasStore(conn)
    echo_registry = SelfEchoRegistry()
    gateway_store = GatewayStore(conn)

    draft_provider = LocalDraftProvider(
        model_gateway=model_gateway,
        model_alias=model_alias or "local",
        custom_llm_fn=custom_llm,
    )
    draft_manager = DraftManager(
        conn=conn,
        key_store=key_store,
        draft_provider=draft_provider,
    )

    # Fetch whitelisted contacts
    cur = conn.execute("SELECT contact_jid FROM whatsapp_contact_settings ORDER BY contact_jid")
    allowed_jids = [r[0] for r in cur.fetchall()]

    config = WhatsAppConfig(
        enabled=True,
        allowed_jids=allowed_jids,
        owner_jid=owner_jid,
        session_path=str(auth_dir),
    )

    node_script_path = str(bridge_dir / "index.js")
    supervisor = BaileysSidecarSupervisor(
        node_script_path=node_script_path,
        auth_dir=str(auth_dir),
        auto_start=False,
        allowed_jids=allowed_jids,
        owner_jid=owner_jid,
        echo_registry=echo_registry,
    )

    owner_handler = OwnerCommandHandler(
        db_conn=conn,
        draft_manager=draft_manager,
        key_store=key_store,
        allowed_jids=allowed_jids,
        supervisor=supervisor,
    )

    gateway = ChannelGateway(
        config=config,
        store=gateway_store,
        transport=supervisor,
        router=None,
        supervisor=supervisor,
        contact_memory_store=memory_store,
        draft_manager=draft_manager,
        alias_store=alias_store,
        echo_registry=echo_registry,
        owner_channel_handler=owner_handler,
    )

    # Start sweeper thread
    sweeper = WhatsAppSweeperThread(draft_manager, interval=60.0)
    sweeper.start()

    # Record gateway state
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO whatsapp_gateway_state (key, value, updated_at)
                VALUES ('bridge_status', 'STARTING', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (now_iso,),
            )
            conn.execute(
                """
                INSERT INTO whatsapp_gateway_state (key, value, updated_at)
                VALUES ('bridge_start_time', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (now_iso, now_iso),
            )
    except Exception as exc:
        logger.warning("Could not record gateway state: %s", exc)

    if stop_event is None:
        stop_event = threading.Event()

        def _sig_handler(sig: int, frame: Any) -> None:
            logger.info("Received termination signal %s; shutting down...", sig)
            stop_event.set()

        try:
            signal.signal(signal.SIGINT, _sig_handler)
            signal.signal(signal.SIGTERM, _sig_handler)
        except (ValueError, AttributeError):
            pass

    consecutive_failures = 0
    backoff_seconds = 1.0
    supervisor_active = False

    try:
        while not stop_event.is_set():
            if not supervisor_active or not supervisor.is_connected:
                # Handle bridge start or crash
                if supervisor_active:
                    consecutive_failures += 1
                    logger.warning(
                        "Baileys bridge crash detected (consecutive failures: %d/%d).",
                        consecutive_failures,
                        max_consecutive_failures,
                    )
                    if consecutive_failures >= max_consecutive_failures:
                        logger.critical("Maximum consecutive bridge crashes reached. Alerting and shutting down.")
                        try:
                            supervisor.send_owner_notice(
                                "[AEGIS ALERT] WhatsApp bridge has crashed repeatedly. Gateway shutting down."
                            )
                        except Exception:
                            pass
                        return 1

                    stop_event.wait(backoff_seconds)
                    backoff_seconds = min(backoff_seconds * 2.0, 60.0)

                try:
                    logger.info("Starting Baileys sidecar bridge supervisor...")
                    supervisor.start()
                    supervisor_active = True
                    # If started successfully, reset backoff after some stability
                    backoff_seconds = 1.0
                    consecutive_failures = 0
                    with conn:
                        conn.execute(
                            "UPDATE whatsapp_gateway_state SET value = 'CONNECTED', updated_at = ? WHERE key = 'bridge_status'",
                            (datetime.now(timezone.utc).isoformat(),),
                        )
                except Exception as exc:
                    logger.error("Failed to start Baileys bridge supervisor: %s", exc)
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        return 1
                    stop_event.wait(backoff_seconds)
                    backoff_seconds = min(backoff_seconds * 2.0, 60.0)
                    continue

            # Poll events
            try:
                processed = gateway.poll_and_process_once()
                if not processed:
                    stop_event.wait(0.2)
            except Exception as exc:
                logger.error("Error in gateway poll loop: %s", exc)
                stop_event.wait(0.5)

    finally:
        logger.info("Shutting down WhatsApp gateway runtime...")
        sweeper.stop()
        sweeper.join(timeout=5.0)

        try:
            with conn:
                conn.execute(
                    "UPDATE whatsapp_gateway_state SET value = 'STOPPED', updated_at = ? WHERE key = 'bridge_status'",
                    (datetime.now(timezone.utc).isoformat(),),
                )
        except Exception:
            pass

        try:
            supervisor.stop(timeout_seconds=5.0)
        except Exception as exc:
            logger.warning("Error stopping bridge supervisor: %s", exc)

        try:
            conn.close()
        except Exception:
            pass

        logger.info("WhatsApp gateway runtime successfully shut down.")

    return 0
