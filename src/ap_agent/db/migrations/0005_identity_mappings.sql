-- 0005_identity_mappings
-- M11E (authentication and hosted deployment): the controlled, database-owned
-- mapping from an external identity-provider organization/user (Clerk) to an
-- internal tenant and `InterfaceRole`. The identity provider proves who the
-- caller is and which organization is active; THIS table is the only source
-- of the internal tenant and role. It is written only by the administrative
-- CLI (migration/admin DSN); the runtime role is granted read access only
-- (`ap_agent.db.roles.grant_schema_access` revokes write privileges on these
-- two tables). No row-level security: the lookup happens before any tenant
-- context exists, by design.
-- Additive only: no earlier migration, table, trigger or policy is edited.
-- Checksum is computed over each '-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===' -delimited
-- statement block, stripped and newline-joined (same algorithm as 0001-0004).
-- Do not reformat statement bodies: doing so changes the checksum recorded
-- for an already-applied migration.

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.identity_organizations
    (
        provider TEXT NOT NULL,
        external_org_id TEXT NOT NULL,
        tenant_id UUID NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),

        PRIMARY KEY (provider, external_org_id),

        -- Exactly one external organization per tenant and provider, and one
        -- tenant per external organization (no cross-tenant aliasing).
        CONSTRAINT identity_organizations_one_org_per_tenant
            UNIQUE (provider, tenant_id),

        CONSTRAINT identity_organizations_target_unique
            UNIQUE (provider, external_org_id, tenant_id),

        CONSTRAINT identity_organizations_tenant_fk
            FOREIGN KEY (tenant_id)
            REFERENCES ap_agent.tenants(tenant_id)
            ON DELETE RESTRICT,

        CONSTRAINT identity_organizations_provider_known
            CHECK (provider IN ('clerk')),

        CONSTRAINT identity_organizations_external_id_format
            CHECK (external_org_id ~ '^[A-Za-z0-9_:.-]{1,128}$'),

        CONSTRAINT identity_organizations_status_known
            CHECK (status IN ('ACTIVE', 'DISABLED')),

        CONSTRAINT identity_organizations_timestamp_order
            CHECK (updated_at >= created_at)
    );

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.identity_mappings
    (
        mapping_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        provider TEXT NOT NULL,
        external_org_id TEXT NOT NULL,
        external_user_id TEXT NOT NULL,
        tenant_id UUID NOT NULL,
        interface_role TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),
        disabled_at TIMESTAMPTZ,

        CONSTRAINT identity_mappings_unique_member
            UNIQUE (provider, external_org_id, external_user_id),

        -- The tenant must be the one the organization is registered to.
        CONSTRAINT identity_mappings_organization_fk
            FOREIGN KEY (provider, external_org_id, tenant_id)
            REFERENCES ap_agent.identity_organizations(provider, external_org_id, tenant_id)
            ON DELETE RESTRICT,

        CONSTRAINT identity_mappings_external_user_format
            CHECK (external_user_id ~ '^[A-Za-z0-9_:.-]{1,128}$'),

        CONSTRAINT identity_mappings_role_known
            CHECK (
                interface_role IN (
                    'TENANT_ADMIN', 'AP_OPERATOR', 'AP_REVIEWER', 'READ_ONLY_AUDITOR'
                )
            ),

        CONSTRAINT identity_mappings_status_known
            CHECK (status IN ('ACTIVE', 'DISABLED')),

        CONSTRAINT identity_mappings_disabled_at_consistent
            CHECK ((status = 'DISABLED') = (disabled_at IS NOT NULL)),

        CONSTRAINT identity_mappings_timestamp_order
            CHECK (updated_at >= created_at)
    );

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX identity_mappings_tenant_index
    ON ap_agent.identity_mappings (tenant_id, status);

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE FUNCTION
        ap_agent.guard_identity_organization_change()
    RETURNS TRIGGER
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION
                'identity_organizations rows are never deleted; disable them instead.'
            USING ERRCODE = '55000';
        END IF;

        IF NEW.provider IS DISTINCT FROM OLD.provider
           OR NEW.external_org_id IS DISTINCT FROM OLD.external_org_id
           OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION
                'identity_organizations identity columns are immutable.'
            USING ERRCODE = '55000';
        END IF;

        RETURN NEW;
    END;
    $$;

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE FUNCTION
        ap_agent.guard_identity_mapping_change()
    RETURNS TRIGGER
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION
                'identity_mappings rows are never deleted; disable them instead.'
            USING ERRCODE = '55000';
        END IF;

        IF NEW.mapping_id IS DISTINCT FROM OLD.mapping_id
           OR NEW.provider IS DISTINCT FROM OLD.provider
           OR NEW.external_org_id IS DISTINCT FROM OLD.external_org_id
           OR NEW.external_user_id IS DISTINCT FROM OLD.external_user_id
           OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION
                'identity_mappings identity columns are immutable.'
            USING ERRCODE = '55000';
        END IF;

        RETURN NEW;
    END;
    $$;

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER identity_organizations_guard
    BEFORE UPDATE OR DELETE
    ON ap_agent.identity_organizations
    FOR EACH ROW
    EXECUTE FUNCTION
        ap_agent.guard_identity_organization_change();

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER identity_mappings_guard
    BEFORE UPDATE OR DELETE
    ON ap_agent.identity_mappings
    FOR EACH ROW
    EXECUTE FUNCTION
        ap_agent.guard_identity_mapping_change();
