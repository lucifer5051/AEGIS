-- AEGIS Trust Core Schema
-- Migration ID: 0005_trust_core_schema
-- Version: 5
-- Name: trust_core_schema
-- Destructive: false
--
-- Canonical Reference:
--   docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md  § 15 & 16
--   docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md  § 16 & 17
--
-- Adds:
--   1. system_autonomy_policy   -- operational autonomy tier per project (or global)
--   2. approval_requests        -- pending/resolved user approval records
--   3. audit_records            -- append-only security event log
--
-- Zero-Secret Invariant: Passwords, OAuth tokens, API keys, and raw user
-- prompts MUST NEVER be stored in audit_records.  The application layer
-- enforces this; the schema cannot enforce it at the SQL level, but it is
-- recorded here as an explicit architectural constraint.


-- ============================================================
-- 1. OPERATIONAL AUTONOMY POLICY
-- ============================================================

CREATE TABLE IF NOT EXISTS system_autonomy_policy (
    policy_id       TEXT PRIMARY KEY,
    project_id      TEXT NULL,                -- NULL = system-global autonomy setting
    autonomy_tier   INTEGER NOT NULL
                    CHECK (autonomy_tier IN (0, 1, 2, 3)),
    -- 0: Strict Manual Approval
    -- 1: Safe / Bounded Execution
    -- 2: Supervised Autonomy
    -- 3: Full Bounded Task Autonomy
    configured_by   TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_autonomy_policy_project_id
    ON system_autonomy_policy (project_id);


-- ============================================================
-- 2. APPROVAL REQUESTS
-- ============================================================

CREATE TABLE IF NOT EXISTS approval_requests (
    request_id          TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL,
    project_id          TEXT NULL,
    action_name         TEXT NOT NULL,
    action_risk_level   TEXT NOT NULL
                        CHECK (action_risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    target_resource     TEXT NOT NULL,
    parameters_summary  TEXT NOT NULL,        -- Human-readable; MUST NOT contain secrets
    status              TEXT NOT NULL
                        CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'REVOKED')),
    decision_actor      TEXT NULL,            -- Who approved/rejected (NULL until decided)
    decision_at         TEXT NULL,            -- ISO-8601 UTC; NULL until decided
    expires_at          TEXT NOT NULL,        -- ISO-8601 UTC
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE
);

-- NOTE: tasks table does not yet exist; the FK to task_id is intentionally
-- deferred until the Task Scheduler schema is implemented.  SQLite foreign
-- key enforcement is not applied to non-existent parent tables until the
-- parent table is created, so this migration will succeed without a tasks
-- table as long as PRAGMA foreign_keys = ON is not set to enforce missing
-- parent tables (SQLite enforces FK only for existing parent tables).

CREATE INDEX IF NOT EXISTS idx_approval_requests_status
    ON approval_requests (status);

CREATE INDEX IF NOT EXISTS idx_approval_requests_project_id
    ON approval_requests (project_id);

CREATE INDEX IF NOT EXISTS idx_approval_requests_expires_at
    ON approval_requests (expires_at);


-- ============================================================
-- 3. AUDIT RECORDS (append-only; rows must never be updated or deleted)
-- ============================================================

CREATE TABLE IF NOT EXISTS audit_records (
    audit_id            TEXT PRIMARY KEY,
    event_type          TEXT NOT NULL
                        CHECK (event_type IN (
                            'AUTHZ_DECISION', 'CAPABILITY_ISSUED', 'CAPABILITY_REVOKED',
                            'APPROVAL_GRANTED', 'APPROVAL_DENIED', 'POLICY_CHANGE',
                            'DESTRUCTIVE_OP', 'PROJECT_PURGE', 'BACKUP_CREATED',
                            'RESTORE_EXECUTED', 'CREDENTIAL_ACCESSED', 'NETWORK_BLOCKED'
                        )),
    actor               TEXT NOT NULL,
    target_entity_type  TEXT NOT NULL,
    target_entity_id    TEXT NOT NULL,
    project_id          TEXT NULL,
    decision            TEXT NOT NULL
                        CHECK (decision IN ('ALLOW', 'DENY', 'ESCALATE')),
    policy_rule_id      TEXT NOT NULL,
    telemetry_json      TEXT NOT NULL,        -- JSON; MUST NOT contain raw secrets
    timestamp           TEXT NOT NULL,        -- ISO-8601 UTC
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_audit_records_event_type
    ON audit_records (event_type);

CREATE INDEX IF NOT EXISTS idx_audit_records_actor
    ON audit_records (actor);

CREATE INDEX IF NOT EXISTS idx_audit_records_decision
    ON audit_records (decision);

CREATE INDEX IF NOT EXISTS idx_audit_records_timestamp
    ON audit_records (timestamp);

CREATE INDEX IF NOT EXISTS idx_audit_records_project_id
    ON audit_records (project_id);
