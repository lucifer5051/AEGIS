"""SAGE WhatsApp Draft Lifecycle and Contact Settings Manager.

Canonical Reference:
    AEGIS Prompt 037 / Phase 9C Draft Approval
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md

Invariants:
- Per-contact isolated encryption for stored drafts using ContactKeyStore DEKs.
- 4-character human-friendly IDs from alphabet (23456789ABCDEFGHJKMNPQRSTUVWXYZ).
- Atomic single-statement state transitions with expected status guards.
- Card sanitization: control/bidi character stripping, newline replacement, length cap (500), leading hash escaping.
- Zero plaintext leak of draft text or contact identity in audit events or logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple

from src.security.contact_key_store import (
    ContactKeyStore,
    normalize_and_validate_contact_jid,
)
from src.security.crypto import AES256GCMCipher, DecryptionError
from src.trust.audit import AuditDecision, AuditEventType, AuditWriter, _check_no_secrets

logger = logging.getLogger(__name__)

DRAFT_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
DRAFT_ID_LENGTH = 4
DEFAULT_EXPIRY_HOURS = 6

# Bidi control characters and zero-width codes
_BIDI_AND_ZERO_WIDTH_RE = re.compile(
    r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF\u061C]"
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_draft_aad(contact_jid: str, draft_id: str) -> bytes:
    return f"aegis:whatsapp_draft:v1:{contact_jid}:{draft_id}".encode("utf-8")


def sanitize_card_text(text: str, max_len: int = 500) -> str:
    """Sanitize untrusted text for safe rendering in an owner approval card.

    - Replaces newlines with ' / '
    - Strips control characters and Unicode bidi/zero-width characters
    - Escapes any leading '#'
    - Bounded to max_len (default 500) with ellipsis
    """
    if not text:
        return ""

    # 1. Strip bidi and zero-width characters
    cleaned = _BIDI_AND_ZERO_WIDTH_RE.sub("", text)

    # 2. Replace newlines with ' / '
    cleaned = re.sub(r"[\r\n]+", " / ", cleaned)

    # 3. Strip control characters (except space)
    cleaned = "".join(
        ch for ch in cleaned if unicodedata.category(ch)[0] != "C" or ch == " "
    )

    cleaned = cleaned.strip()

    # 4. Escape leading '#'
    if cleaned.startswith("#"):
        cleaned = "\\" + cleaned

    # 5. Cap length
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "..."

    return cleaned


def format_approval_card(
    draft_id: str,
    label: Optional[str],
    contact_jid: str,
    style_mode: str,
    their_text: str,
    draft_text: str,
    is_placeholder: bool = True,
    expires_in_hours: int = DEFAULT_EXPIRY_HOURS,
    has_injection_warning: bool = False,
) -> str:
    """Render the standardized WhatsApp approval card posted to the owner's self-chat."""
    last2 = contact_jid.split("@")[0][-2:] if "@" in contact_jid else contact_jid[-2:]
    display_label = label if label else "Contact"
    sanitized_their = sanitize_card_text(their_text, 500)
    sanitized_draft = sanitize_card_text(draft_text, 500)
    tag = "  (placeholder)" if is_placeholder else ""
    d_id = draft_id.upper()
    warning_line = "⚠️ WARNING: Possible prompt injection detected in contact message!\n" if has_injection_warning else ""
    return (
        f"[AEGIS] Draft #{d_id}{tag}\n"
        f"{warning_line}"
        f"From: {display_label} (...{last2})   mode: {style_mode}\n"
        f'They wrote: "{sanitized_their}"\n'
        f'Draft: "{sanitized_draft}"\n'
        f"#approve {d_id} | #edit {d_id} <text> | #reject {d_id}   expires in {expires_in_hours}h"
    )


@dataclass(frozen=True)
class DraftContext:
    """Isolated, contact-scoped context for draft generation."""

    contact_jid: str
    label: Optional[str]
    style_mode: str  # 'normal' | 'banter'
    inbound_text: str
    recent_messages: List[Dict[str, str]]


class DraftProvider(Protocol):
    """Protocol for generating draft replies."""

    def draft(self, ctx: DraftContext) -> str:
        """Produce a proposed response string given an isolated contact context."""
        ...


class PlaceholderDraftProvider:
    """Deterministic placeholder draft provider used until local LLM drafting is integrated."""

    def draft(self, ctx: DraftContext) -> str:
        return "Thank you for your message. (placeholder)"


def generate_draft_id(conn: sqlite3.Connection) -> str:
    """Generate a random 4-character ID from DRAFT_ALPHABET, collision-checked."""
    for _ in range(50):
        cand = "".join(secrets.choice(DRAFT_ALPHABET) for _ in range(DRAFT_ID_LENGTH))
        cur = conn.execute(
            "SELECT count(*) FROM whatsapp_drafts WHERE UPPER(draft_id) = UPPER(?) AND status IN ('pending', 'approved', 'sending')",
            (cand,),
        )
        if cur.fetchone()[0] == 0:
            return cand
    raise RuntimeError("Failed to generate unique draft ID after 50 attempts")


class DraftManager:
    """Lifecycle manager for WhatsApp drafts, contact settings, and approval states."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        key_store: ContactKeyStore,
        draft_provider: Optional[DraftProvider] = None,
        audit_writer: Optional[AuditWriter] = None,
    ) -> None:
        self._conn = conn
        self._key_store = key_store
        self._provider = draft_provider or PlaceholderDraftProvider()
        self._audit_writer = audit_writer
        self._lock = threading.Lock()

    def _audit(
        self,
        reason: str,
        target_id: str,
        telemetry: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._audit_writer is not None:
            try:
                self._audit_writer.write(
                    event_type=AuditEventType.AUTHZ_DECISION,
                    actor="whatsapp_draft_manager",
                    target_entity_type="whatsapp_draft",
                    target_entity_id=target_id,
                    decision=AuditDecision.ALLOW,
                    policy_rule_id=reason,
                    telemetry=telemetry or {},
                )
            except Exception as exc:
                logger.error("Failed to write audit event for %s: %s", reason, exc)

    # -------------------------------------------------------------------------
    # PAUSE STATE MANAGEMENT
    # -------------------------------------------------------------------------

    def is_paused(self) -> bool:
        """Check whether draft generation and sending are globally paused."""
        cur = self._conn.execute(
            "SELECT value FROM whatsapp_gateway_state WHERE key = 'is_paused'"
        )
        row = cur.fetchone()
        return bool(row and row[0] == "1")

    def set_paused(self, paused: bool) -> None:
        """Set the global gateway pause flag."""
        val = "1" if paused else "0"
        now = _now_utc()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO whatsapp_gateway_state (key, value, updated_at)
                VALUES ('is_paused', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (val, now),
            )
        self._audit(
            "gateway_paused" if paused else "gateway_resumed",
            target_id="global_gateway",
            telemetry={"is_paused": paused},
        )

    # -------------------------------------------------------------------------
    # CONTACT SETTINGS MANAGEMENT
    # -------------------------------------------------------------------------

    def get_contact_settings(self, contact_jid: str) -> Dict[str, Any]:
        """Retrieve settings (label, style_mode) for a contact."""
        canonical = normalize_and_validate_contact_jid(contact_jid)
        cur = self._conn.execute(
            "SELECT label, style_mode FROM whatsapp_contact_settings WHERE contact_jid = ?",
            (canonical,),
        )
        row = cur.fetchone()
        if row:
            return {"contact_jid": canonical, "label": row[0], "style_mode": row[1]}
        return {"contact_jid": canonical, "label": None, "style_mode": "normal"}

    def list_contact_settings(self) -> List[Dict[str, Any]]:
        """List all contact settings ordered by canonical JID."""
        cur = self._conn.execute(
            "SELECT contact_jid, label, style_mode, updated_at FROM whatsapp_contact_settings ORDER BY contact_jid ASC"
        )
        results = []
        for row in cur.fetchall():
            results.append(
                {
                    "contact_jid": row[0],
                    "label": row[1],
                    "style_mode": row[2],
                    "updated_at": row[3],
                }
            )
        return results

    def add_or_update_contact_setting(
        self,
        contact_jid: str,
        label: Optional[str] = None,
        style_mode: str = "normal",
    ) -> None:
        """Persist or update contact settings."""
        canonical = normalize_and_validate_contact_jid(contact_jid)
        if style_mode not in ("normal", "banter"):
            raise ValueError(f"Invalid style_mode '{style_mode}'. Must be 'normal' or 'banter'.")
        now = _now_utc()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO whatsapp_contact_settings (contact_jid, label, style_mode, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(contact_jid) DO UPDATE SET
                    label = COALESCE(excluded.label, whatsapp_contact_settings.label),
                    style_mode = excluded.style_mode,
                    updated_at = excluded.updated_at
                """,
                (canonical, label, style_mode, now),
            )
        last2 = canonical.split("@")[0][-2:]
        self._audit("contact_added", target_id=f"***{last2}", telemetry={"label": label, "mode": style_mode})

    def remove_contact_setting(self, contact_jid: str) -> None:
        """Remove contact setting and cancel pending drafts."""
        canonical = normalize_and_validate_contact_jid(contact_jid)
        with self._conn:
            self._conn.execute(
                "DELETE FROM whatsapp_contact_settings WHERE contact_jid = ?",
                (canonical,),
            )
        self.cancel_drafts_for_contact(canonical)
        last2 = canonical.split("@")[0][-2:]
        self._audit("contact_removed", target_id=f"***{last2}")

    def set_contact_mode(self, contact_jid: str, style_mode: str) -> bool:
        """Change the style mode for a contact."""
        canonical = normalize_and_validate_contact_jid(contact_jid)
        if style_mode not in ("normal", "banter"):
            raise ValueError(f"Invalid style_mode '{style_mode}'")
        now = _now_utc()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE whatsapp_contact_settings
                SET style_mode = ?, updated_at = ?
                WHERE contact_jid = ?
                """,
                (style_mode, now, canonical),
            )
            if cur.rowcount == 0:
                # If not explicitly in settings table, insert default with updated mode
                self._conn.execute(
                    """
                    INSERT INTO whatsapp_contact_settings (contact_jid, label, style_mode, updated_at)
                    VALUES (?, NULL, ?, ?)
                    """,
                    (canonical, style_mode, now),
                )
        last2 = canonical.split("@")[0][-2:]
        self._audit("mode_changed", target_id=f"***{last2}", telemetry={"style_mode": style_mode})
        return True

    # -------------------------------------------------------------------------
    # DRAFT CREATION AND MANAGEMENT
    # -------------------------------------------------------------------------

    def create_draft_for_inbound(
        self,
        contact_jid: str,
        inbound_message_id: str,
        inbound_text: str,
        recent_messages: Optional[List[Dict[str, str]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create an encrypted draft reply for an inbound message."""
        canonical = normalize_and_validate_contact_jid(contact_jid)

        # 1. Gate on paused state
        if self.is_paused():
            logger.info("Draft creation skipped: gateway is paused.")
            return None

        # 2. Check rate limits: max 3 new drafts per contact per 10m, max 20 pending overall
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        ten_mins_ago = (now_dt - timedelta(minutes=10)).isoformat()

        cur = self._conn.execute(
            "SELECT count(*) FROM whatsapp_drafts WHERE contact_jid = ? AND created_at >= ?",
            (canonical, ten_mins_ago),
        )
        if cur.fetchone()[0] >= 3:
            logger.warning("Draft limit reached for contact %s (>= 3 in 10m)", canonical)
            self._audit("draft_limit_exceeded", target_id="rate_limit", telemetry={"limit": "contact_10m"})
            return None

        cur = self._conn.execute(
            "SELECT count(*) FROM whatsapp_drafts WHERE status = 'pending'"
        )
        if cur.fetchone()[0] >= 20:
            logger.warning("Global pending draft limit reached (>= 20 pending)")
            self._audit("draft_limit_exceeded", target_id="rate_limit", telemetry={"limit": "global_pending"})
            return None

        # 3. Supersede older pending drafts for this contact
        with self._conn:
            self._conn.execute(
                """
                UPDATE whatsapp_drafts
                SET status = 'superseded', decided_at = ?
                WHERE contact_jid = ? AND status = 'pending'
                """,
                (now_iso, canonical),
            )

        # 4. Resolve settings and draft text
        settings = self.get_contact_settings(canonical)
        label = settings.get("label")
        style_mode = settings.get("style_mode", "normal")

        ctx = DraftContext(
            contact_jid=canonical,
            label=label,
            style_mode=style_mode,
            inbound_text=inbound_text,
            recent_messages=recent_messages or [],
        )
        draft_text = self._provider.draft(ctx)
        _check_no_secrets(draft_text)

        # 5. Encrypt draft with contact's isolated DEK
        dek = self._key_store.get_or_create_key(canonical)
        draft_id = generate_draft_id(self._conn)
        aad = _build_draft_aad(canonical, draft_id)
        cipher = AES256GCMCipher(dek)
        ciphertext, nonce = cipher.encrypt(draft_text.encode("utf-8"), aad=aad)
        text_sha = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
        expires_at = (now_dt + timedelta(hours=DEFAULT_EXPIRY_HOURS)).isoformat()

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO whatsapp_drafts (
                    draft_id, contact_jid, inbound_message_id, draft_ciphertext,
                    draft_nonce, text_sha256, status, created_at, expires_at,
                    decided_at, sent_message_id, edit_count
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, 0)
                """,
                (
                    draft_id,
                    canonical,
                    inbound_message_id,
                    ciphertext,
                    nonce,
                    text_sha,
                    now_iso,
                    expires_at,
                ),
            )

        from src.gateway.local_draft_provider import check_prompt_injection

        has_inj, _ = check_prompt_injection(inbound_text)
        is_ph = isinstance(self._provider, PlaceholderDraftProvider) or "(placeholder)" in draft_text
        card = format_approval_card(
            draft_id=draft_id,
            label=label,
            contact_jid=canonical,
            style_mode=style_mode,
            their_text=inbound_text,
            draft_text=draft_text,
            is_placeholder=is_ph,
            expires_in_hours=DEFAULT_EXPIRY_HOURS,
            has_injection_warning=has_inj,
        )

        last2 = canonical.split("@")[0][-2:]
        self._audit("draft_created", target_id=draft_id, telemetry={"contact_masked": f"***{last2}", "injection_warning": has_inj})

        return {
            "draft_id": draft_id,
            "contact_jid": canonical,
            "label": label,
            "style_mode": style_mode,
            "draft_text": draft_text,
            "text_sha256": text_sha,
            "card": card,
            "expires_at": expires_at,
            "has_injection_warning": has_inj,
        }

    def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a draft by ID with expiry check."""
        if not draft_id:
            return None
        cur = self._conn.execute(
            """
            SELECT d.draft_id, d.contact_jid, d.inbound_message_id, d.draft_ciphertext,
                   d.draft_nonce, d.text_sha256, d.status, d.created_at, d.expires_at,
                   d.decided_at, d.sent_message_id, d.edit_count, s.label, s.style_mode
            FROM whatsapp_drafts d
            LEFT JOIN whatsapp_contact_settings s ON d.contact_jid = s.contact_jid
            WHERE UPPER(d.draft_id) = UPPER(?)
            """,
            (draft_id.strip(),),
        )
        row = cur.fetchone()
        if not row:
            return None

        status = row[6]
        expires_at_str = row[8]
        now_iso = _now_utc()

        # Check expiry
        if status == "pending" and expires_at_str < now_iso:
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE whatsapp_drafts SET status = 'expired', decided_at = ? WHERE UPPER(draft_id) = UPPER(?) AND status = 'pending'",
                    (now_iso, draft_id.strip()),
                )
                if cur.rowcount > 0:
                    status = "expired"
                    self._audit("draft_expired", target_id=draft_id.upper())

        return {
            "draft_id": row[0],
            "contact_jid": row[1],
            "inbound_message_id": row[2],
            "draft_ciphertext": row[3],
            "draft_nonce": row[4],
            "text_sha256": row[5],
            "status": status,
            "created_at": row[7],
            "expires_at": row[8],
            "decided_at": row[9],
            "sent_message_id": row[10],
            "edit_count": row[11],
            "label": row[12],
            "style_mode": row[13] or "normal",
        }

    def edit_draft(self, draft_id: str, new_text: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """Edit a pending draft's plaintext and re-encrypt with the contact DEK.

        Returns:
            (success: bool, error_or_none: Optional[str], card_or_none: Optional[str])
        """
        draft = self.get_draft(draft_id)
        if not draft:
            return False, f"Draft #{draft_id.upper()} not found.", None
        if draft["status"] != "pending":
            return (
                False,
                f"Draft #{draft_id.upper()} cannot be edited (status is '{draft['status']}').",
                None,
            )

        text_str = str(new_text or "").strip()
        if not text_str:
            return False, "Draft text cannot be empty.", None
        if len(text_str) > 4000:
            return False, "Draft text exceeds 4000 characters.", None

        _check_no_secrets(text_str)

        contact_jid = draft["contact_jid"]
        dek = self._key_store.get_key(contact_jid)
        if dek is None:
            return False, "Could not retrieve key for contact.", None

        d_id = draft["draft_id"]
        aad = _build_draft_aad(contact_jid, d_id)
        cipher = AES256GCMCipher(dek)
        new_cipher, new_nonce = cipher.encrypt(text_str.encode("utf-8"), aad=aad)
        new_hash = hashlib.sha256(text_str.encode("utf-8")).hexdigest()

        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE whatsapp_drafts
                SET draft_ciphertext = ?, draft_nonce = ?, text_sha256 = ?, edit_count = edit_count + 1
                WHERE UPPER(draft_id) = UPPER(?) AND status = 'pending'
                """,
                (new_cipher, new_nonce, new_hash, d_id),
            )
            if cur.rowcount == 0:
                return False, f"Draft #{d_id.upper()} was modified or decided concurrently.", None

        is_ph = "(placeholder)" in text_str
        card = format_approval_card(
            draft_id=d_id,
            label=draft.get("label"),
            contact_jid=contact_jid,
            style_mode=draft.get("style_mode", "normal"),
            their_text="[inbound message]",
            draft_text=text_str,
            is_placeholder=is_ph,
        )

        self._audit("draft_edited", target_id=d_id.upper(), telemetry={"edit_count": draft["edit_count"] + 1})
        return True, None, card

    def reject_draft(self, draft_id: str) -> Tuple[bool, str]:
        """Atomically transition a draft from pending to rejected."""
        d_id = draft_id.strip().upper()
        now_iso = _now_utc()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE whatsapp_drafts
                SET status = 'rejected', decided_at = ?
                WHERE UPPER(draft_id) = UPPER(?) AND status = 'pending'
                """,
                (now_iso, d_id),
            )
            if cur.rowcount == 0:
                draft = self.get_draft(d_id)
                st = draft["status"] if draft else "not found"
                return False, f"[AEGIS] Draft #{d_id} cannot be rejected (status: {st})."

        self._audit("draft_rejected", target_id=d_id)
        return True, f"[AEGIS] Draft #{d_id} rejected."

    def cancel_draft(self, draft_id: str) -> bool:
        """Cancel an individual draft."""
        d_id = draft_id.strip().upper()
        now_iso = _now_utc()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE whatsapp_drafts
                SET status = 'cancelled', decided_at = ?
                WHERE UPPER(draft_id) = UPPER(?) AND status = 'pending'
                """,
                (now_iso, d_id),
            )
            return cur.rowcount > 0

    def cancel_drafts_for_contact(self, contact_jid: str) -> int:
        """Cancel all pending drafts for a specific contact."""
        canonical = normalize_and_validate_contact_jid(contact_jid)
        now_iso = _now_utc()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE whatsapp_drafts
                SET status = 'cancelled', decided_at = ?
                WHERE contact_jid = ? AND status = 'pending'
                """,
                (now_iso, canonical),
            )
            count = cur.rowcount
        if count > 0:
            last2 = canonical.split("@")[0][-2:]
            self._audit("drafts_cancelled", target_id=f"***{last2}", telemetry={"count": count})
        return count

    def list_pending_drafts(self) -> List[Dict[str, Any]]:
        """List active pending drafts (re-evaluating expiry)."""
        now_iso = _now_utc()
        cur = self._conn.execute(
            """
            SELECT d.draft_id, d.contact_jid, d.created_at, d.expires_at, s.label
            FROM whatsapp_drafts d
            LEFT JOIN whatsapp_contact_settings s ON d.contact_jid = s.contact_jid
            WHERE d.status = 'pending'
            ORDER BY d.created_at ASC
            """
        )
        active = []
        expired_ids = []
        for row in cur.fetchall():
            d_id, c_jid, cr_at, exp_at, label = row[0], row[1], row[2], row[3], row[4]
            if exp_at < now_iso:
                expired_ids.append(d_id)
            else:
                active.append(
                    {
                        "draft_id": d_id,
                        "contact_jid": c_jid,
                        "created_at": cr_at,
                        "expires_at": exp_at,
                        "label": label or "Contact",
                    }
                )

        if expired_ids:
            with self._conn:
                for eid in expired_ids:
                    self._conn.execute(
                        "UPDATE whatsapp_drafts SET status = 'expired', decided_at = ? WHERE UPPER(draft_id) = UPPER(?)",
                        (now_iso, eid),
                    )
                    self._audit("draft_expired", target_id=eid.upper())

        return active
