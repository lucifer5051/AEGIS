-- AEGIS Domain Schema Foundation - Conversations & Messages
-- Migration ID: 0003_conversations_schema
-- Version: 3
-- Name: conversations_schema
-- Destructive: false
--
-- Implements channel-affine conversations and the message model.
-- Scope: conversations, messages — and their indexes.
--
-- Dependency Order: projects (from 0002) → conversations → messages
--
-- Canonical Reference: docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md §10
-- Architecture Invariants:
--   - Channel affinity is a hard invariant: responses return to originating channel.
--   - Conversations may be project-scoped (project_id FK) or unscoped (NULL).
--   - ON DELETE SET NULL for conversations.project_id (conversation history survives
--     project deletion; orphaned conversations are recoverable user data).
--   - ON DELETE CASCADE for messages.conversation_id (messages purged with conversation).
--   - message_seq enforces strict ordering per conversation (UNIQUE constraint).
--   - No credentials or secrets in raw_payload_json or content fields.
--   - All timestamps: UTC ISO-8601 text.


-- ============================================================
-- DOMAIN 7: CONVERSATIONS
-- ============================================================
-- Channel-affine conversation containers. Each conversation
-- is associated with exactly one channel. Cross-channel
-- message mirroring is prohibited by architecture.
--
-- project_id IS NULL: unscoped/global conversation (e.g. system CLI)
-- project_id NOT NULL: project-scoped conversation
-- ON DELETE SET NULL: conversation history is user data and must
--   survive project deletion (orphaned conversations remain queryable).

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id          TEXT PRIMARY KEY,
    project_id               TEXT NULL,
    channel                  TEXT NOT NULL
                             CHECK (channel IN ('desktop', 'whatsapp', 'system_cli')),
    channel_conversation_id  TEXT NOT NULL,
    title                    TEXT NULL,
    status                   TEXT NOT NULL DEFAULT 'ACTIVE'
                             CHECK (status IN ('ACTIVE', 'IDLE', 'CLOSED')),
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_project_id
    ON conversations (project_id);

CREATE INDEX IF NOT EXISTS idx_conversations_channel
    ON conversations (channel);

CREATE INDEX IF NOT EXISTS idx_conversations_status
    ON conversations (status);

CREATE INDEX IF NOT EXISTS idx_conversations_created_at
    ON conversations (created_at);

CREATE INDEX IF NOT EXISTS idx_conversations_project_channel
    ON conversations (project_id, channel);


-- ============================================================
-- DOMAIN 8: MESSAGES
-- ============================================================
-- Individual turns within a conversation. message_seq provides
-- strict, deterministic ordering within each conversation.
-- UNIQUE(conversation_id, message_seq) prevents duplicate positions.
--
-- ON DELETE CASCADE: messages are fully owned by their conversation.
-- No credential material may appear in content or raw_payload_json.

CREATE TABLE IF NOT EXISTS messages (
    message_id        TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL,
    role              TEXT NOT NULL
                      CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    channel           TEXT NOT NULL
                      CHECK (channel IN ('desktop', 'whatsapp', 'system_cli')),
    message_seq       INTEGER NOT NULL,
    content           TEXT NOT NULL,
    delivery_status   TEXT NOT NULL DEFAULT 'QUEUED'
                      CHECK (delivery_status IN ('QUEUED', 'DELIVERED', 'FAILED', 'READ')),
    raw_payload_json  TEXT NULL,
    tokens_consumed   INTEGER NOT NULL DEFAULT 0
                      CHECK (tokens_consumed >= 0),
    created_at        TEXT NOT NULL,
    FOREIGN KEY (conversation_id)
        REFERENCES conversations (conversation_id) ON DELETE CASCADE,
    UNIQUE (conversation_id, message_seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_seq
    ON messages (conversation_id, message_seq);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
    ON messages (conversation_id, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_role
    ON messages (role);

CREATE INDEX IF NOT EXISTS idx_messages_delivery_status
    ON messages (delivery_status);
