-- Disposable local/CI PostgreSQL role bootstrap (task §9).
--
-- Creates a migration-owner role (may create schemas/tables/policies/
-- triggers) and a runtime application role that is explicitly NOT
-- SUPERUSER and NOT BYPASSRLS (task §5: "Integration tests must use a
-- runtime role without SUPERUSER or BYPASSRLS privileges.").
--
-- Passwords come from environment substitution performed by
-- docker-compose (see docker-compose.yml's `migrate` service, which
-- writes AP_AGENT_POSTGRES_MIGRATION_DSN/AP_AGENT_POSTGRES_DSN using the
-- same values). This file itself contains no real secret: the values
-- below are placeholders for disposable, non-production use.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ap_agent_migrator') THEN
        CREATE ROLE ap_agent_migrator WITH LOGIN PASSWORD 'ap_agent_migrator_dev_password' CREATEROLE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ap_agent_app') THEN
        CREATE ROLE ap_agent_app WITH LOGIN PASSWORD 'ap_agent_app_dev_password' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

GRANT ALL ON DATABASE ap_agent TO ap_agent_migrator;
