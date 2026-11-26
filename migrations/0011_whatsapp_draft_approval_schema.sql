-- SAGE WhatsApp Draft Approval and Contact Settings Schema
-- Migration ID: 0011_whatsapp_draft_approval_schema
-- Version: 11
-- Name: whatsapp_draft_approval_schema
-- Destructive: false
--
-- Canonical Reference:
--   AEGIS Phase 9C Draft-Approval Architecture
--
-- Adds:
--   1. whatsapp_contact_settings -- Per-contact settings (label, style_mode).
--   2. whatsapp_drafts           -- Stored draft replies encrypted per contact.
--   3. whatsapp_gateway_state    -- Persistent gateway key-value state (e.g. is_paused).

CREATE TABLE IF NOT EXISTS whatsapp_contact_settings (
    contact_jid     TEXT PRIMARY KEY,
    label           TEXT,
    style_mode      TEXT NOT NULL DEFAULT 'normal' CHECK (style_mode IN ('normal', 'banter')),
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS whatsapp_drafts (
    draft_id            TEXT PRIMARY KEY,
    contact_jid         TEXT NOT NULL,
    inbound_message_id  TEXT NOT NULL,
    draft_ciphertext    BLOB NOT NULL,
    draft_nonce         BLOB NOT NULL,
    text_sha256         TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'sending', 'sent', 'failed', 'rejected', 'expired', 'superseded', 'cancelled')),
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    decided_at          TEXT,
    sent_message_id     TEXT,
    edit_count          INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_drafts_contact
    ON whatsapp_drafts (contact_jid, status);

CREATE INDEX IF NOT EXISTS idx_whatsapp_drafts_created
    ON whatsapp_drafts (created_at);

CREATE TABLE IF NOT EXISTS whatsapp_gateway_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
