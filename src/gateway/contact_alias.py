"""SAGE Remote Control Gateway -- WhatsApp Contact LID-to-Phone Alias Store.

Canonical Reference:
    D1: Canonical Identity Resolution (LID -> Canonical PN)
    migrations/0010_whatsapp_contact_alias_schema.sql
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Collection, Optional

CANONICAL_PN_REGEX = re.compile(r"^\d{7,15}@s\.whatsapp\.net$")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContactAliasStore:
    """Manages mapping between WhatsApp Linked Identity (LID) addresses and canonical phone numbers."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_canonical_pn(self, lid: str) -> Optional[str]:
        """Look up canonical phone-number JID for a given @lid."""
        if not lid or not lid.endswith("@lid"):
            return None
        cur = self._conn.execute(
            "SELECT canonical_pn FROM whatsapp_contact_aliases WHERE lid = ?",
            (lid.strip(),),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def learn_alias(
        self,
        lid: str,
        canonical_pn: str,
        allowed_jids: Collection[str],
    ) -> bool:
        """Learn and persist an alias if and only if canonical_pn is already whitelisted.

        Prevents alias poisoning by strangers.
        """
        if not lid or not lid.endswith("@lid"):
            return False
        if not canonical_pn or not CANONICAL_PN_REGEX.match(canonical_pn):
            return False
        if canonical_pn not in allowed_jids:
            # Prevent alias poisoning by stranger
            return False

        now = _now_utc()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO whatsapp_contact_aliases (lid, canonical_pn, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lid) DO UPDATE SET
                    canonical_pn = excluded.canonical_pn,
                    updated_at = excluded.updated_at
                """,
                (lid.strip(), canonical_pn.strip(), now, now),
            )
        return True
