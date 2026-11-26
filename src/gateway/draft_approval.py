"""SAGE WhatsApp Draft Approval and Dispatch Controller.

Canonical Reference:
    AEGIS Prompt 037 / Phase 9C
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md

Security Invariants:
- This module is the ONLY authorized application caller permitted to construct
  SendAuthorization tokens or call its factory.
- Double-approval race safe: atomic status transition from 'pending' to 'approved'
  ensures exactly one execution path wins.
- Tamper check: draft text is decrypted and re-hashed before minting SendAuthorization.
- Contact whitelist is re-verified at execution time.
- Never automatically retries failed sends.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Collection, Dict, Optional, Tuple

from src.gateway.baileys_sidecar import BaileysSidecarSupervisor
from src.gateway.drafts import DraftManager, _build_draft_aad
from src.gateway.send_authorization import create_send_authorization
from src.security.contact_key_store import ContactKeyStore
from src.security.crypto import AES256GCMCipher
from src.trust.audit import AuditDecision, AuditEventType, AuditWriter

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def approve_and_send_draft(
    draft_id: str,
    draft_manager: DraftManager,
    supervisor: BaileysSidecarSupervisor,
    key_store: ContactKeyStore,
    allowed_jids: Collection[str],
    audit_writer: Optional[AuditWriter] = None,
) -> Tuple[bool, str]:
    """Approve a pending draft, verify invariants, mint SendAuthorization, and dispatch.

    Returns:
        (success: bool, status_message_for_owner: str)
    """
    d_id = draft_id.strip().upper()

    def _audit(event: str, target: str, telemetry: Optional[Dict[str, Any]] = None) -> None:
        if audit_writer is not None:
            try:
                audit_writer.write(
                    event_type=AuditEventType.AUTHZ_DECISION,
                    actor="whatsapp_draft_approval",
                    target_entity_type="whatsapp_draft",
                    target_entity_id=target,
                    decision=AuditDecision.ALLOW if "failed" not in event and "refused" not in event else AuditDecision.DENY,
                    policy_rule_id=event,
                    telemetry=telemetry or {},
                )
            except Exception as exc:
                logger.error("Audit write failed in draft approval: %s", exc)

    # 1. Gate: Gateway pause flag
    if draft_manager.is_paused():
        _audit("command_refused", d_id, {"reason": "gateway_paused"})
        return False, "[AEGIS] Gateway is paused. Resume with #resume before approving drafts."

    # 2. Retrieve draft with expiry check
    draft = draft_manager.get_draft(d_id)
    if not draft:
        _audit("command_refused", d_id, {"reason": "draft_not_found"})
        return False, f"[AEGIS] Draft #{d_id} not found."

    current_status = draft["status"]
    if current_status != "pending":
        _audit("command_refused", d_id, {"reason": "status_not_pending", "status": current_status})
        return False, f"[AEGIS] Draft #{d_id} cannot be approved (status: {current_status})."

    contact_jid = draft["contact_jid"]
    last2 = contact_jid.split("@")[0][-2:] if "@" in contact_jid else contact_jid[-2:]
    label = draft.get("label") or "Contact"

    # 3. Gate: Contact still whitelisted at send time
    if contact_jid not in allowed_jids:
        draft_manager.cancel_draft(d_id)
        _audit("command_refused", d_id, {"reason": "contact_not_whitelisted", "contact": f"***{last2}"})
        return False, f"[AEGIS] Contact {label} (...{last2}) is no longer whitelisted. Draft #{d_id} cancelled."

    # 4. Atomic Transition: pending -> approved
    now_iso = _now_utc()
    conn = draft_manager._conn
    with draft_manager._lock:
        with conn:
            cur = conn.execute(
                """
                UPDATE whatsapp_drafts
                SET status = 'approved', decided_at = ?
                WHERE UPPER(draft_id) = UPPER(?) AND status = 'pending'
                """,
                (now_iso, d_id),
            )
            if cur.rowcount == 0:
                # Race condition: another thread/command claimed it
                _audit("command_refused", d_id, {"reason": "concurrent_approval_race"})
                return False, f"[AEGIS] Draft #{d_id} was already decided by a concurrent request."

    _audit("draft_approved", d_id, {"contact": f"***{last2}"})

    # 5. Decrypt stored draft ciphertext using contact DEK
    raw_dek = key_store.get_key(contact_jid)
    if raw_dek is None:
        with conn:
            conn.execute(
                "UPDATE whatsapp_drafts SET status = 'failed' WHERE UPPER(draft_id) = UPPER(?) AND status = 'approved'",
                (d_id,),
            )
        _audit("draft_failed", d_id, {"reason": "key_unavailable"})
        return False, f"[AEGIS] Failed to retrieve encryption key for {label} (...{last2})."

    aad = _build_draft_aad(contact_jid, draft["draft_id"])
    try:
        cipher = AES256GCMCipher(raw_dek)
        plaintext_bytes = cipher.decrypt(
            draft["draft_ciphertext"],
            draft["draft_nonce"],
            aad=aad,
        )
        plaintext = plaintext_bytes.decode("utf-8")
    except Exception as exc:
        with conn:
            conn.execute(
                "UPDATE whatsapp_drafts SET status = 'failed' WHERE UPPER(draft_id) = UPPER(?) AND status = 'approved'",
                (d_id,),
            )
        _audit("draft_failed", d_id, {"reason": "decryption_failure"})
        logger.error("Failed to decrypt draft #%s: %s", d_id, exc)
        return False, f"[AEGIS] Decryption failure for Draft #{d_id}."

    # 6. Tamper check: Plaintext re-hash must match stored text_sha256
    computed_hash = hashlib.sha256(plaintext_bytes).hexdigest()
    if computed_hash != draft["text_sha256"]:
        with conn:
            conn.execute(
                "UPDATE whatsapp_drafts SET status = 'failed' WHERE UPPER(draft_id) = UPPER(?) AND status = 'approved'",
                (d_id,),
            )
        _audit("draft_failed", d_id, {"reason": "tamper_detected"})
        logger.critical("Tamper detected for draft #%s: hash mismatch!", d_id)
        return False, f"[AEGIS] SECURITY ALERT: Tamper detected for Draft #{d_id}! Send aborted."

    # 7. Transition: approved -> sending
    with conn:
        cur = conn.execute(
            "UPDATE whatsapp_drafts SET status = 'sending' WHERE UPPER(draft_id) = UPPER(?) AND status = 'approved'",
            (d_id,),
        )
        if cur.rowcount == 0:
            _audit("command_refused", d_id, {"reason": "status_not_approved"})
            return False, f"[AEGIS] Draft #{d_id} was already transitioned out of approved status."

    # 8. Mint SendAuthorization token
    auth = create_send_authorization(
        approval_id=d_id,
        recipient_jid=contact_jid,
        text=plaintext,
    )

    # 9. Send via supervisor
    try:
        res = supervisor.send_message(
            recipient_jid=contact_jid,
            text=plaintext,
            authorization=auth,
            timeout=30.0,
        )
        sent_id = res.get("id") or res.get("message_id") or "sent"
        with conn:
            conn.execute(
                """
                UPDATE whatsapp_drafts
                SET status = 'sent', sent_message_id = ?
                WHERE UPPER(draft_id) = UPPER(?) AND status = 'sending'
                """,
                (str(sent_id), d_id),
            )
        _audit("draft_sent", d_id, {"contact": f"***{last2}"})
        return True, f"[AEGIS] Draft #{d_id} sent to {label} (...{last2})."
    except Exception as exc:
        with conn:
            conn.execute(
                "UPDATE whatsapp_drafts SET status = 'failed' WHERE UPPER(draft_id) = UPPER(?) AND status = 'sending'",
                (d_id,),
            )
        _audit("draft_failed", d_id, {"error": exc.__class__.__name__})
        logger.error("Outbound send failed for draft #%s: %s", d_id, exc)
        return False, f"[AEGIS] Draft #{d_id} failed to send: {exc}"
