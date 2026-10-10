-- SAGE Durable Workflow Engine & Task Scheduler Schema
-- Migration ID: 0007_workflow_scheduler_schema
-- Version: 7
-- Name: workflow_scheduler_schema
-- Destructive: false
--
-- Canonical Reference:
--   docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 11, § 12
--   docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 11, § 12
--
-- Adds:
--   1. workflow_definitions     -- Versioned workflow DAG definitions
--   2. workflow_runs            -- Persistent workflow execution instances
--   3. schedules                -- Durable one-time, recurring, and event-triggered schedules
--   4. schedule_executions      -- Deduplicated audit trail of scheduled runs
--
-- Zero-Secret Invariant: Passwords, tokens, API keys, and sensitive credentials
-- MUST NEVER be persisted in metadata, parameters, results, or checkpoint snapshots.


-- ============================================================
-- 1. WORKFLOW DEFINITIONS
-- ============================================================

CREATE TABLE IF NOT EXISTS workflow_definitions (
    workflow_id                 TEXT NOT NULL,
    version                     INTEGER NOT NULL DEFAULT 1,
    project_id                  TEXT NULL,
    name                        TEXT NOT NULL,
    description                 TEXT NULL,
    is_active                   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    dag_specification_json      TEXT NOT NULL,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    PRIMARY KEY (workflow_id, version),
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workflow_definitions_workflow_id
    ON workflow_definitions (workflow_id);

CREATE INDEX IF NOT EXISTS idx_workflow_definitions_project_id
    ON workflow_definitions (project_id);

CREATE INDEX IF NOT EXISTS idx_workflow_definitions_is_active
    ON workflow_definitions (is_active);


-- ============================================================
-- 2. WORKFLOW RUNS (INSTANCES)
-- ============================================================

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id                      TEXT PRIMARY KEY,
    workflow_id                 TEXT NOT NULL,
    workflow_version            INTEGER NOT NULL DEFAULT 1,
    project_id                  TEXT NULL,
    task_id                     TEXT NULL,
    status                      TEXT NOT NULL
                                CHECK (status IN (
                                    'PENDING', 'RUNNING', 'PAUSED',
                                    'COMPLETED', 'FAILED', 'CANCELLED'
                                )),
    triggered_by                TEXT NOT NULL DEFAULT 'user',
    inputs_json                 TEXT NOT NULL DEFAULT '{}',
    outputs_json                TEXT NOT NULL DEFAULT '{}',
    step_states_json            TEXT NOT NULL DEFAULT '{}',
    error_summary               TEXT NULL,
    started_at                  TEXT NOT NULL,
    completed_at                TEXT NULL,
    FOREIGN KEY (workflow_id, workflow_version)
        REFERENCES workflow_definitions (workflow_id, version) ON DELETE RESTRICT,
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE,
    FOREIGN KEY (task_id)
        REFERENCES tasks (task_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_id
    ON workflow_runs (workflow_id);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_project_id
    ON workflow_runs (project_id);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_task_id
    ON workflow_runs (task_id);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_status
    ON workflow_runs (status);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_started_at
    ON workflow_runs (started_at);


-- ============================================================
-- 3. SCHEDULES
-- ============================================================

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id                 TEXT PRIMARY KEY,
    project_id                  TEXT NULL,
    workflow_id                 TEXT NULL,
    target_action               TEXT NOT NULL,
    schedule_type               TEXT NOT NULL
                                CHECK (schedule_type IN (
                                    'ONE_SHOT', 'CRON_RECURRING', 'EVENT_TRIGGERED'
                                )),
    cron_expression             TEXT NULL,
    interval_seconds            INTEGER NULL,
    trigger_condition_json      TEXT NULL,
    next_run_at                 TEXT NOT NULL,
    last_run_at                 TEXT NULL,
    missed_run_policy           TEXT NOT NULL
                                CHECK (missed_run_policy IN (
                                    'RUN_ONCE', 'SKIP', 'FAIL_ALERT'
                                )),
    status                      TEXT NOT NULL DEFAULT 'ACTIVE'
                                CHECK (status IN (
                                    'ACTIVE', 'PAUSED', 'CANCELLED', 'EXPIRED'
                                )),
    is_enabled                  INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    parameters_json             TEXT NOT NULL DEFAULT '{}',
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    FOREIGN KEY (project_id)
        REFERENCES projects (project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_schedules_project_id
    ON schedules (project_id);

CREATE INDEX IF NOT EXISTS idx_schedules_workflow_id
    ON schedules (workflow_id);

CREATE INDEX IF NOT EXISTS idx_schedules_status
    ON schedules (status);

CREATE INDEX IF NOT EXISTS idx_schedules_next_run_at
    ON schedules (next_run_at);


-- ============================================================
-- 4. SCHEDULE EXECUTIONS (DEDUPLICATION & AUDIT)
-- ============================================================

CREATE TABLE IF NOT EXISTS schedule_executions (
    execution_id                TEXT PRIMARY KEY,
    schedule_id                 TEXT NOT NULL,
    run_id                      TEXT NULL,
    scheduled_for               TEXT NOT NULL,
    triggered_at                TEXT NOT NULL,
    status                      TEXT NOT NULL
                                CHECK (status IN (
                                    'PENDING', 'RUNNING', 'COMPLETED',
                                    'FAILED', 'SKIPPED'
                                )),
    result_summary              TEXT NULL,
    created_at                  TEXT NOT NULL,
    completed_at                TEXT NULL,
    FOREIGN KEY (schedule_id)
        REFERENCES schedules (schedule_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id)
        REFERENCES workflow_runs (run_id) ON DELETE SET NULL,
    CONSTRAINT uq_schedule_execution_dedup UNIQUE (schedule_id, scheduled_for)
);

CREATE INDEX IF NOT EXISTS idx_schedule_executions_schedule_id
    ON schedule_executions (schedule_id);

CREATE INDEX IF NOT EXISTS idx_schedule_executions_run_id
    ON schedule_executions (run_id);

CREATE INDEX IF NOT EXISTS idx_schedule_executions_status
    ON schedule_executions (status);
