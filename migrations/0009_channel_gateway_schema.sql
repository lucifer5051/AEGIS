-- SAGE Remote Control Gateway Schema
-- Migration ID: 0009_channel_gateway_schema
-- Version: 9
-- Name: channel_gateway_schema
-- Destructive: false
--
-- Canonical Reference:
--   docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 3 (Channels)
--   docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
--
-- Adds:
--   1. gateway_updates          -- Inbound updates from remote channels (Telegram, etc.) with state machine
--   2. gateway_outbound_messages -- Outbound messages and replies queued for remote delivery
--
-- Zero-Secret Invariant: Update content, parameters, and error summaries MUST NEVER store raw tokens,
-- passwords, API keys, or credentials.


-- ============================================================
-- 1. GATEWAY INBOUND UPDATES
-- ============================================================

CREATE TABLE IF NOT EXISTS gateway_updates (
    update_id               TEXT PRIMARY KEY,
    channel                 TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp', 'desktop', 'system_cli')),
    channel_update_id       TEXT NOT NULL,
    sender_id               TEXT NOT NULL,
    chat_id                 TEXT NOT NULL,
    chat_type               TEXT NOT NULL DEFAULT 'private',
    message_type            TEXT NOT NULL DEFAULT 'text',
    raw_text                TEXT NULL,
    state                   TEXT NOT NULL DEFAULT 'RECEIVED'
                                CHECK (state IN ('RECEIVED', 'VALIDATED', 'QUEUED', 'PROCESSING', 'AWAITING_APPROVAL', 'COMPLETED', 'REJECTED', 'FAILED', 'CANCELLED')),
    task_id                 TEXT NULL REFERENCES tasks (task_id) ON DELETE SET NULL,
    retry_count             INTEGER NOT NULL DEFAULT 0,
    max_retries             INTEGER NOT NULL DEFAULT 3,
    error_summary           TEXT NULL,
    metadata_json           TEXT NOT NULL DEFAULT '{}',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    CONSTRAINT uq_channel_update UNIQUE (channel, channel_update_id)
);

CREATE INDEX IF NOT EXISTS idx_gateway_updates_state
    ON gateway_updates (state);

CREATE INDEX IF NOT EXISTS idx_gateway_updates_channel_sender
    ON gateway_updates (channel, sender_id);

CREATE INDEX IF NOT EXISTS idx_gateway_updates_task_id
    ON gateway_updates (task_id);

CREATE INDEX IF NOT EXISTS idx_gateway_updates_created_at
    ON gateway_updates (created_at);


-- ============================================================
-- 2. GATEWAY OUTBOUND MESSAGES
-- ============================================================

CREATE TABLE IF NOT EXISTS gateway_outbound_messages (
    message_id              TEXT PRIMARY KEY,
    channel                 TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp', 'desktop', 'system_cli')),
    recipient_id            TEXT NOT NULL,
    inbound_update_id       TEXT NULL,
    task_id                 TEXT NULL,
    content                 TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'PENDING'
                                CHECK (status IN ('PENDING', 'SENT', 'FAILED', 'UNCERTAIN')),
    attempts                INTEGER NOT NULL DEFAULT 0,
    max_attempts            INTEGER NOT NULL DEFAULT 3,
    last_attempt_at         TEXT NULL,
    error_message           TEXT NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gateway_outbound_status
    ON gateway_outbound_messages (status);

CREATE INDEX IF NOT EXISTS idx_gateway_outbound_recipient
    ON gateway_outbound_messages (channel, recipient_id);
