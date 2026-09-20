-- AEGIS Storage Foundation Initial Migration
-- Migration ID: 0001_initial_storage
-- Version: 1
-- Name: initial_storage
-- Destructive: false
--
-- Initializes the minimal storage_metadata key-value table.
-- Note: schema_migrations is bootstrapped directly by the migration runner,
-- not by this migration. Zero domain tables are defined here.

CREATE TABLE IF NOT EXISTS storage_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
