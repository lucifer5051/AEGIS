-- SAGE Task Execution and State Recovery Schema
-- Migration ID: 0006_task_execution_recovery_schema
-- Version: 6
-- Name: task_execution_recovery_schema
-- Destructive: false
--
-- Canonical Reference:
--   docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md  § 27
--   docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
--
-- Adds:
--   1. supervisor_sessions    -- Lifecycle of supervisor execution sessions
--   2. tasks                  -- Durable task records with strict state machine
--   3. task_steps             -- Ordered steps within a task execution plan
--   4. task_checkpoints       -- Idempotent snapshots for crash recovery & resume
--
-- Zero-Secret Invariant: Passwords, tokens, API keys, and sensitive credentials
-- MUST NEVER be persisted in metadata, parameters, results, or checkpoint snapshots.


-- ============================================================
-- 1. SUPERVISOR SESSIONS
-- ============================================================

CREATE TABLE IF NOT EXISTS supervisor_sessions (
    session_id          TEXT PRIMARY KEY,
    status              TEXT NOT NULL
                        CHECK (status IN ('ACTIVE', 'PAUSED', 'COMPLETED', 'FAILED', 'TERMINATED')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    metadata_json       TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_supervisor_sessions_status
    ON supervisor_sessions (status);

CREATE INDEX IF NOT EXISTS idx_supervisor_sessions_created_at
    ON supervisor_sessions (created_at);


-- ============================================================
-- 2. TASKS
-- ============================================================

CREATE TABLE IF NOT EXISTS tasks (
    task_id             TEXT PRIMARY KEY,
    session_id          TEXT NULL,
    project_id          TEXT NULL,
    title               TEXT NOT NULL,
    state               TEXT NOT NULL
                        CHECK (state IN (
                            'SUBMITTED', 'PLANNING', 'AWAITING_APPROVAL',
                            'EXECUTING', 'COMPLETED', 'FAILED', 'CANCELLED'
                        )),
    priority            INTEGER NOT NULL DEFAULT 100,
    routing_intent      TEXT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    completed_at        TEXT NULL,
    error_summary       TEXT NULL,
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (session_id)
        REFERENCES supervisor_sessions (session_id) ON DELETE SET NULL,
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tasks_session_id
    ON tasks (session_id);

CREATE INDEX IF NOT EXISTS idx_tasks_project_id
    ON tasks (project_id);

CREATE INDEX IF NOT EXISTS idx_tasks_state
    ON tasks (state);

CREATE INDEX IF NOT EXISTS idx_tasks_created_at
    ON tasks (created_at);


-- ============================================================
-- 3. TASK STEPS
-- ============================================================

CREATE TABLE IF NOT EXISTS task_steps (
    step_id             TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL,
    step_index          INTEGER NOT NULL,
    action_name         TEXT NOT NULL,
    tool_name           TEXT NULL,
    state               TEXT NOT NULL
                        CHECK (state IN (
                            'PENDING', 'RUNNING', 'COMPLETED',
                            'FAILED', 'SKIPPED', 'AWAITING_APPROVAL'
                        )),
    is_idempotent       INTEGER NOT NULL DEFAULT 0,
    idempotency_key     TEXT NULL,
    capability_id       TEXT NULL,
    arguments_json      TEXT NOT NULL DEFAULT '{}',
    result_summary      TEXT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    completed_at        TEXT NULL,
    FOREIGN KEY (task_id)
        REFERENCES tasks (task_id) ON DELETE CASCADE,
    CONSTRAINT uq_task_steps_task_index UNIQUE (task_id, step_index)
);

CREATE INDEX IF NOT EXISTS idx_task_steps_task_id
    ON task_steps (task_id);

CREATE INDEX IF NOT EXISTS idx_task_steps_state
    ON task_steps (state);

CREATE INDEX IF NOT EXISTS idx_task_steps_idempotency_key
    ON task_steps (idempotency_key);


-- ============================================================
-- 4. TASK CHECKPOINTS
-- ============================================================

CREATE TABLE IF NOT EXISTS task_checkpoints (
    checkpoint_id       TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL,
    step_id             TEXT NULL,
    checkpoint_seq      INTEGER NOT NULL,
    snapshot_json       TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (task_id)
        REFERENCES tasks (task_id) ON DELETE CASCADE,
    FOREIGN KEY (step_id)
        REFERENCES task_steps (step_id) ON DELETE SET NULL,
    CONSTRAINT uq_task_checkpoints_task_seq UNIQUE (task_id, checkpoint_seq)
);

CREATE INDEX IF NOT EXISTS idx_task_checkpoints_task_id
    ON task_checkpoints (task_id);

CREATE INDEX IF NOT EXISTS idx_task_checkpoints_step_id
    ON task_checkpoints (step_id);
