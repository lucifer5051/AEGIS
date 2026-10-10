-- SAGE Notification Subsystem Schema
-- Migration ID: 0008_notifications_schema
-- Version: 8
-- Name: notifications_schema
-- Destructive: false
--
-- Canonical Reference:
--   docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 9 (Notifications)
--   docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 13
--
-- Adds:
--   1. notifications         -- Typed, prioritized event notifications with delivery state
--   2. notification_attempts -- Per-delivery-attempt audit trail with bounded retries
--
-- Zero-Secret Invariant: Notification content MUST NEVER include passwords, tokens,
-- API keys, private keys, raw secrets, or any credential-class data.
-- Delivery adapters must redact sensitive content before persisting or transmitting.


-- ============================================================
-- 1. NOTIFICATIONS
-- ============================================================

CREATE TABLE IF NOT EXISTS notifications (
    notification_id         TEXT PRIMARY KEY,
    event_type              TEXT NOT NULL CHECK (event_type IN (
                                'APPROVAL_REQUIRED',
                                'APPROVAL_EXPIRED',
                                'APPROVAL_GRANTED',
                                'APPROVAL_REJECTED',
                                'WORKFLOW_COMPLETED',
                                'WORKFLOW_FAILED',
                                'WORKFLOW_PAUSED',
                                'SCHEDULE_FAILED',
                                'SCHEDULE_MISSED',
                                'RESOURCE_CONSTRAINT',
                                'RECOVERY_EXHAUSTED',
                                'INTERVENTION_REQUIRED',
                                'GENERAL_INFO'
                            )),
    priority                TEXT NOT NULL DEFAULT 'P2' CHECK (priority IN ('P0', 'P1', 'P2', 'P3', 'P4')),
    title                   TEXT NOT NULL,
    body                    TEXT NOT NULL,
    source_type             TEXT NULL,          -- 'workflow', 'schedule', 'approval', 'system'
    source_id               TEXT NULL,          -- run_id, schedule_id, request_id, etc.
    project_id              TEXT NULL,
    dedup_key               TEXT NULL,          -- Deduplication grouping key
    status                  TEXT NOT NULL DEFAULT 'PENDING'
                                CHECK (status IN ('PENDING', 'DELIVERED', 'FAILED', 'ACKNOWLEDGED', 'DISMISSED', 'SUPPRESSED')),
    delivery_attempts       INTEGER NOT NULL DEFAULT 0,
    max_delivery_attempts   INTEGER NOT NULL DEFAULT 3,
    acknowledged_at         TEXT NULL,
    dismissed_at            TEXT NULL,
    expires_at              TEXT NULL,          -- Notification auto-expires (e.g. approval window)
    metadata_json           TEXT NOT NULL DEFAULT '{}',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_status
    ON notifications (status);

CREATE INDEX IF NOT EXISTS idx_notifications_event_type
    ON notifications (event_type);

CREATE INDEX IF NOT EXISTS idx_notifications_priority
    ON notifications (priority, status);

CREATE INDEX IF NOT EXISTS idx_notifications_source
    ON notifications (source_type, source_id);

CREATE INDEX IF NOT EXISTS idx_notifications_dedup_key
    ON notifications (dedup_key);

CREATE INDEX IF NOT EXISTS idx_notifications_project_id
    ON notifications (project_id);

CREATE INDEX IF NOT EXISTS idx_notifications_created_at
    ON notifications (created_at);


-- ============================================================
-- 2. NOTIFICATION DELIVERY ATTEMPTS
-- ============================================================

CREATE TABLE IF NOT EXISTS notification_attempts (
    attempt_id              TEXT PRIMARY KEY,
    notification_id         TEXT NOT NULL REFERENCES notifications (notification_id) ON DELETE CASCADE,
    attempt_number          INTEGER NOT NULL,
    adapter                 TEXT NOT NULL,          -- 'local_log', 'desktop', future adapters
    attempted_at            TEXT NOT NULL,
    succeeded               INTEGER NOT NULL DEFAULT 0 CHECK (succeeded IN (0, 1)),
    error_message           TEXT NULL,
    response_summary        TEXT NULL               -- Opaque adapter response summary (no secrets)
);

CREATE INDEX IF NOT EXISTS idx_notification_attempts_notification_id
    ON notification_attempts (notification_id);

CREATE INDEX IF NOT EXISTS idx_notification_attempts_attempted_at
    ON notification_attempts (attempted_at);
