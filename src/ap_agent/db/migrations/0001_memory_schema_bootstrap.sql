-- 0001_memory_schema_bootstrap
-- Source: notebook Phase 7 Cell 3 (BOOTSTRAP_STATEMENTS), verbatim.
-- Checksum is computed over each '-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===' -delimited
-- statement block, stripped and newline-joined, exactly as the notebook's
-- calculate_migration_checksum() does. Do not reformat statement bodies:
-- doing so changes the checksum recorded for an already-applied migration.

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE SCHEMA IF NOT EXISTS ap_agent;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE IF NOT EXISTS
        ap_agent.schema_migrations
    (
        migration_id TEXT PRIMARY KEY,
        checksum CHAR(64) NOT NULL,
        description TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),
        applied_by TEXT NOT NULL
            DEFAULT current_user
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    COMMENT ON SCHEMA ap_agent IS
        'Accounts Payable Agent operational memory.';
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    COMMENT ON TABLE
        ap_agent.schema_migrations
    IS
        'Immutable record of applied database migrations.';
    