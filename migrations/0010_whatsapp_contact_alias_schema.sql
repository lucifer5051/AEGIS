-- SAGE WhatsApp Contact LID-to-Phone Alias Schema
-- Migration ID: 0010_whatsapp_contact_alias_schema
-- Version: 10
-- Name: whatsapp_contact_alias_schema
-- Destructive: false
--
-- Canonical Reference:
--   D1: Canonical Identity Resolution (LID -> Canonical PN)
--
-- Adds:
--   1. whatsapp_contact_aliases -- Maps WhatsApp Linked Identity (LID) addresses
--                                  to canonical phone-number JIDs (<digits>@s.whatsapp.net).

CREATE TABLE IF NOT EXISTS whatsapp_contact_aliases (
    lid                     TEXT PRIMARY KEY,
    canonical_pn            TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_contact_aliases_pn
    ON whatsapp_contact_aliases (canonical_pn);
