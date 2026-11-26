"""SAGE Remote Control Gateway -- Owner 'Message Yourself' Channel Interface.

Canonical Reference:
    D4: Safe Owner Channel
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)


class OwnerChannelHandler(Protocol):
    """Protocol for handling commands and approvals from the owner's self-chat."""

    def handle_owner_message(self, message_id: str, text: str) -> None:
        """Process an inbound message received in the owner's 'message yourself' chat."""
        ...


class DefaultOwnerChannelHandler:
    """Default handler for owner messages, emitting an audit event and zero text retention."""

    def __init__(self, audit_callback: Optional[Callable[[str, int], Any]] = None) -> None:
        self._audit_callback = audit_callback

    def handle_owner_message(self, message_id: str, text: str) -> None:
        text_len = len(text) if text else 0
        logger.info(
            "Owner message received (id=%s, length=%d). Zero text retained in contact memory.",
            message_id,
            text_len,
        )
        if self._audit_callback is not None:
            self._audit_callback(message_id, text_len)


class OwnerCommandHandler:
    """Processes administrative commands sent by the owner from their self-chat.

    Supported commands:
    - #approve <ID> / #a <ID>
    - #edit <ID> <text>
    - #reject <ID> / #r <ID>
    - #list
    - #contacts
    - #mode <index_or_number> <normal|banter>
    - #pause
    - #resume
    - #help

    Guarantees:
    - Zero owner command text is routed to the LLM or stored in contact memory.
    - Status updates and responses are delivered strictly via send_owner_notice().
    """

    def __init__(
        self,
        db_conn: Any,
        draft_manager: Any,
        key_store: Any,
        allowed_jids: Any,
        supervisor: Optional[Any] = None,
        audit_writer: Optional[Any] = None,
        audit_callback: Optional[Callable[[str, int], Any]] = None,
    ) -> None:
        self._conn = db_conn
        self._draft_manager = draft_manager
        self._key_store = key_store
        self._allowed_jids = allowed_jids
        self._supervisor = supervisor
        self._audit_writer = audit_writer
        self._audit_callback = audit_callback

    def _get_allowed_jids(self) -> Collection[str]:
        if callable(self._allowed_jids):
            return self._allowed_jids()
        if isinstance(self._allowed_jids, (set, list, tuple)):
            return set(self._allowed_jids)
        if self._supervisor is not None and hasattr(self._supervisor, "_allowed_jids"):
            return set(self._supervisor._allowed_jids)
        # Fallback to contacts in database
        cur = self._conn.execute("SELECT contact_jid FROM whatsapp_contact_settings")
        return {r[0] for r in cur.fetchall()}

    def handle_owner_message(self, message_id: str, text: str) -> None:
        text_str = (text or "").strip()
        text_len = len(text_str)

        if self._audit_callback is not None:
            self._audit_callback(message_id, text_len)

        if not text_str:
            return

        cmd_lower = text_str.lower()
        parts = text_str.split()
        first_token = parts[0].lower() if parts else ""

        # Normalize prefix: allow both '#cmd' and 'cmd' for convenience, but # is standard
        cmd_name = first_token[1:] if first_token.startswith("#") else first_token

        reply: Optional[str] = None

        if cmd_name in ("approve", "a"):
            if len(parts) < 2:
                reply = "[AEGIS] Usage: #approve <ID> (or #a <ID>)"
            else:
                draft_id = parts[1].strip().upper()
                from src.gateway.draft_approval import approve_and_send_draft

                success, status_msg = approve_and_send_draft(
                    draft_id=draft_id,
                    draft_manager=self._draft_manager,
                    supervisor=self._supervisor,
                    key_store=self._key_store,
                    allowed_jids=self._get_allowed_jids(),
                    audit_writer=self._audit_writer,
                )
                reply = status_msg

        elif cmd_name == "edit":
            tokens = text_str.split(maxsplit=2)
            if len(tokens) < 3:
                reply = "[AEGIS] Usage: #edit <ID> <new text>"
            else:
                draft_id = tokens[1].strip().upper()
                new_text = tokens[2].strip()
                success, err, card = self._draft_manager.edit_draft(draft_id, new_text)
                if not success:
                    reply = f"[AEGIS] {err}"
                else:
                    reply = f"[AEGIS] Draft #{draft_id} updated:\n{card}"

        elif cmd_name in ("reject", "r"):
            if len(parts) < 2:
                reply = "[AEGIS] Usage: #reject <ID> (or #r <ID>)"
            else:
                draft_id = parts[1].strip().upper()
                success, msg = self._draft_manager.reject_draft(draft_id)
                reply = msg

        elif cmd_name == "list":
            drafts = self._draft_manager.list_pending_drafts()
            if not drafts:
                reply = "[AEGIS] No pending drafts."
            else:
                lines = [f"[AEGIS] Pending drafts ({len(drafts)}):"]
                for d in drafts:
                    c_jid = d["contact_jid"]
                    last2 = c_jid.split("@")[0][-2:] if "@" in c_jid else c_jid[-2:]
                    label = d.get("label") or "Contact"
                    exp = d.get("expires_at", "")[:16].replace("T", " ")
                    lines.append(f"• #{d['draft_id']} for {label} (...{last2}) [exp: {exp} UTC]")
                lines.append("Use #approve <ID> or #reject <ID>.")
                reply = "\n".join(lines)

        elif cmd_name == "contacts":
            cur = self._conn.execute(
                "SELECT contact_jid, label, style_mode FROM whatsapp_contact_settings ORDER BY contact_jid"
            )
            rows = cur.fetchall()
            if not rows:
                reply = "[AEGIS] No whitelisted WhatsApp contacts configured."
            else:
                lines = [f"[AEGIS] Whitelisted contacts ({len(rows)}/12):"]
                for idx, r in enumerate(rows, 1):
                    c_jid, label, style_mode = r[0], r[1] or "No Label", r[2] or "normal"
                    last2 = c_jid.split("@")[0][-2:] if "@" in c_jid else c_jid[-2:]
                    lines.append(f"{idx}. {label} (...{last2}) - mode: {style_mode}")
                lines.append("Use #mode <id|pn> <normal|banter> to change style.")
                reply = "\n".join(lines)

        elif cmd_name == "mode":
            tokens = text_str.split(maxsplit=2)
            if len(tokens) < 3:
                reply = "[AEGIS] Usage: #mode <index_or_number> <normal|banter>"
            else:
                target_ref = tokens[1].strip()
                new_mode = tokens[2].strip().lower()
                if new_mode not in ("normal", "banter"):
                    reply = "[AEGIS] Invalid mode. Must be 'normal' or 'banter'."
                else:
                    cur = self._conn.execute(
                        "SELECT contact_jid, label FROM whatsapp_contact_settings ORDER BY contact_jid"
                    )
                    rows = cur.fetchall()
                    target_jid: Optional[str] = None
                    target_label: Optional[str] = None

                    # Check if target_ref is a 1-based index
                    if target_ref.isdigit() and 1 <= int(target_ref) <= len(rows):
                        idx = int(target_ref) - 1
                        target_jid = rows[idx][0]
                        target_label = rows[idx][1]
                    else:
                        clean_ref = "".join(ch for ch in target_ref if ch.isdigit())
                        for r in rows:
                            pn = r[0].split("@")[0]
                            if target_ref.lower() == r[0].lower() or (clean_ref and pn.endswith(clean_ref)):
                                target_jid = r[0]
                                target_label = r[1]
                                break

                    if not target_jid:
                        reply = f"[AEGIS] Contact '{target_ref}' not found in whitelisted contacts."
                    else:
                        with self._conn:
                            self._conn.execute(
                                "UPDATE whatsapp_contact_settings SET style_mode = ? WHERE contact_jid = ?",
                                (new_mode, target_jid),
                            )
                        last2 = target_jid.split("@")[0][-2:]
                        lbl = target_label or "Contact"
                        reply = f"[AEGIS] Set drafting mode for {lbl} (...{last2}) to '{new_mode}'."

        elif cmd_name == "pause":
            self._draft_manager.set_paused(True)
            reply = "[AEGIS] WhatsApp gateway paused. Inbound messages will be received but drafting and outbound sends are stopped."

        elif cmd_name == "resume":
            self._draft_manager.set_paused(False)
            reply = "[AEGIS] WhatsApp gateway resumed."

        elif cmd_name == "help":
            reply = (
                "[AEGIS WhatsApp Commands]\n"
                "#approve <ID> (or #a) - Approve & send draft\n"
                "#edit <ID> <text> - Edit draft text\n"
                "#reject <ID> (or #r) - Reject draft\n"
                "#list - View pending drafts\n"
                "#contacts - View whitelisted contacts\n"
                "#mode <#|pn> <normal|banter> - Switch drafting style\n"
                "#pause - Pause drafting & sending\n"
                "#resume - Resume gateway operations\n"
                "#help - Show this guide"
            )

        else:
            reply = "[AEGIS] Unknown command. Send #help for available commands."

        if reply and self._supervisor is not None and hasattr(self._supervisor, "send_owner_notice"):
            try:
                self._supervisor.send_owner_notice(reply)
            except Exception as exc:
                logger.error("Failed to send owner notice: %s", exc)
