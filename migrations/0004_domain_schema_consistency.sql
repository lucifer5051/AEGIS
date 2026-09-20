-- AEGIS Domain Schema Consistency Correction
-- Migration ID: 0004_domain_schema_consistency
-- Version: 4
-- Name: domain_schema_consistency
-- Destructive: false
--
-- Canonical Reference: docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
-- Architectural Corrections:
--   1. Normalizes legacy / non-canonical trust level codes to canonical M0-M4.
--   2. Reconstructs conversations to enforce ON DELETE CASCADE on project_id.
--   3. Reconstructs messages to maintain strict foreign key topology.
--   4. Recreates all indexes and unique constraints on conversations and messages.
--
-- Explicit Project Cascade Ownership Matrix:
-- +-----------------------+----------------------+------------------------------------------+
-- | Entity                | project_id nullable  | On project delete                        |
-- +-----------------------+----------------------+------------------------------------------+
-- | memory_facts          | Yes (NULL = global)  | Cascade only for project-scoped records  |
-- | memory_evidence       | Yes (NULL = global)  | Follow parent memory / cascade if scoped |
-- | user_preferences      | Yes (NULL = global)  | Project-scoped entries removed (CASCADE) |
-- | entities              | Yes (NULL = global)  | Defined per ownership model (CASCADE)    |
-- | entity_relationships  | Yes (NULL = global)  | Remove project-owned relations (CASCADE) |
-- | conversations         | Yes (NULL = global)  | CASCADE project conversations            |
-- | messages              | No project FK        | Cascade through parent conversation      |
-- +-----------------------+----------------------+------------------------------------------+


-- ============================================================
-- STEP 1: NORMALIZE LEGACY TRUST-LEVEL VALUES
-- ============================================================

UPDATE memory_facts
SET trust_level = 'M0'
WHERE trust_level IN ('M0_TRANSIENT', 'M0_RAW', 'M0_RAW_OBSERVATION');

UPDATE memory_facts
SET trust_level = 'M1'
WHERE trust_level IN ('M1_AGENT_CLAIM', 'M1_CLAIM', 'M1_HYPOTHESIS');

UPDATE memory_facts
SET trust_level = 'M2'
WHERE trust_level IN ('M2_TOOL_SUPPORTED', 'M2_TOOL', 'M2_CORROBORATED');

UPDATE memory_facts
SET trust_level = 'M3'
WHERE trust_level IN ('M3_VERIFIED', 'M3_MULTI_VERIFIED');

UPDATE memory_facts
SET trust_level = 'M4'
WHERE trust_level IN ('M4_AXIOMATIC', 'M4_USER_CONFIRMED', 'M4_CONFIRMED');

UPDATE user_preferences
SET trust_level = 'M0'
WHERE trust_level IN ('M0_TRANSIENT', 'M0_RAW', 'M0_RAW_OBSERVATION');

UPDATE user_preferences
SET trust_level = 'M1'
WHERE trust_level IN ('M1_AGENT_CLAIM', 'M1_CLAIM', 'M1_HYPOTHESIS');

UPDATE user_preferences
SET trust_level = 'M2'
WHERE trust_level IN ('M2_TOOL_SUPPORTED', 'M2_TOOL', 'M2_CORROBORATED');

UPDATE user_preferences
SET trust_level = 'M3'
WHERE trust_level IN ('M3_VERIFIED', 'M3_MULTI_VERIFIED');

UPDATE user_preferences
SET trust_level = 'M4'
WHERE trust_level IN ('M4_AXIOMATIC', 'M4_USER_CONFIRMED', 'M4_CONFIRMED');

UPDATE entity_relationships
SET trust_level = 'M0'
WHERE trust_level IN ('M0_TRANSIENT', 'M0_RAW', 'M0_RAW_OBSERVATION');

UPDATE entity_relationships
SET trust_level = 'M1'
WHERE trust_level IN ('M1_AGENT_CLAIM', 'M1_CLAIM', 'M1_HYPOTHESIS');

UPDATE entity_relationships
SET trust_level = 'M2'
WHERE trust_level IN ('M2_TOOL_SUPPORTED', 'M2_TOOL', 'M2_CORROBORATED');

UPDATE entity_relationships
SET trust_level = 'M3'
WHERE trust_level IN ('M3_VERIFIED', 'M3_MULTI_VERIFIED');

UPDATE entity_relationships
SET trust_level = 'M4'
WHERE trust_level IN ('M4_AXIOMATIC', 'M4_USER_CONFIRMED', 'M4_CONFIRMED');


-- ============================================================
-- STEP 2: SAFE RECONSTRUCTION OF CONVERSATIONS & MESSAGES
-- ============================================================

-- 2a. Rename conversations to _conversations_old
ALTER TABLE conversations RENAME TO _conversations_old;

-- 2b. Create new conversations table with ON DELETE CASCADE on project_id
CREATE TABLE conversations (
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
        REFERENCES projects (project_id) ON DELETE CASCADE
);

-- 2c. Copy all existing conversations
INSERT INTO conversations (
    conversation_id, project_id, channel, channel_conversation_id,
    title, status, created_at, updated_at
)
SELECT
    conversation_id, project_id, channel, channel_conversation_id,
    title, status, created_at, updated_at
FROM _conversations_old;

-- 2d. Create _messages_new referencing the new conversations table
CREATE TABLE _messages_new (
    message_id        TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL,
    role              TEXT NOT NULL
                      CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    channel           TEXT NOT NULL
                      CHECK (channel IN ('desktop', 'whatsapp', 'system_cli')),
    message_seq       INTEGER NOT NULL,
    content           TEXT NOT NULL,
    delivery_status   TEXT NOT NULL DEFAULT 'DELIVERED'
                      CHECK (delivery_status IN ('QUEUED', 'DELIVERED', 'FAILED', 'READ')),
    raw_payload_json  TEXT NULL,
    tokens_consumed   INTEGER NOT NULL DEFAULT 0
                      CHECK (tokens_consumed >= 0),
    created_at        TEXT NOT NULL,
    FOREIGN KEY (conversation_id)
        REFERENCES conversations (conversation_id) ON DELETE CASCADE,
    UNIQUE (conversation_id, message_seq)
);

-- 2e. Copy all existing messages into _messages_new
INSERT INTO _messages_new (
    message_id, conversation_id, role, channel, message_seq, content,
    delivery_status, raw_payload_json, tokens_consumed, created_at
)
SELECT
    message_id, conversation_id, role, channel, message_seq, content,
    delivery_status, raw_payload_json, tokens_consumed, created_at
FROM messages;

-- 2f. Drop legacy messages table
DROP TABLE messages;

-- 2g. Drop _conversations_old table
DROP TABLE _conversations_old;

-- 2h. Rename _messages_new to messages
ALTER TABLE _messages_new RENAME TO messages;


-- ============================================================
-- STEP 3: RECREATE INDEXES
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_conversations_project_id
    ON conversations (project_id);

CREATE INDEX IF NOT EXISTS idx_conversations_channel
    ON conversations (channel);

CREATE INDEX IF NOT EXISTS idx_conversations_status
    ON conversations (status);

CREATE INDEX IF NOT EXISTS idx_conversations_created_at
    ON conversations (created_at);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
    ON messages (conversation_id);

CREATE INDEX IF NOT EXISTS idx_messages_role
    ON messages (role);

CREATE INDEX IF NOT EXISTS idx_messages_created_at
    ON messages (created_at);

CREATE INDEX IF NOT EXISTS idx_messages_delivery_status
    ON messages (delivery_status);
