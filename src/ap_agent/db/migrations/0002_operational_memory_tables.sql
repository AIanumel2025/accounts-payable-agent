-- 0002_operational_memory_tables
-- Source: notebook Phase 7 Cell 4 (MEMORY_MIGRATION_STATEMENTS), verbatim.
-- Checksum is computed over each '-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===' -delimited
-- statement block, stripped and newline-joined, exactly as the notebook's
-- canonical_migration_sql() does. Do not reformat statement bodies: doing
-- so changes the checksum recorded for an already-applied migration.

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.tenants
    (
        tenant_id UUID PRIMARY KEY,
        tenant_key TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),

        CONSTRAINT tenants_key_format
            CHECK (
                tenant_key ~
                '^[a-z0-9][a-z0-9_-]{2,62}$'
            ),

        CONSTRAINT tenants_name_not_empty
            CHECK (
                length(trim(display_name)) > 0
            ),

        CONSTRAINT tenants_status_format
            CHECK (
                status ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT tenants_timestamp_order
            CHECK (
                updated_at >= created_at
            )
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.workflow_instances
    (
        workflow_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        batch_id UUID NOT NULL,
        document_id UUID NOT NULL,
        source_document_sha256 CHAR(64) NOT NULL,
        source_name TEXT NOT NULL,
        current_phase TEXT NOT NULL,
        current_status TEXT NOT NULL,
        review_required BOOLEAN NOT NULL
            DEFAULT FALSE,
        lock_version BIGINT NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),
        completed_at TIMESTAMPTZ,

        CONSTRAINT workflow_tenant_fk
            FOREIGN KEY (tenant_id)
            REFERENCES ap_agent.tenants(tenant_id)
            ON DELETE RESTRICT,

        CONSTRAINT workflow_tenant_identity
            UNIQUE (tenant_id, workflow_id),

        CONSTRAINT workflow_document_identity
            UNIQUE (tenant_id, document_id),

        CONSTRAINT workflow_hash_format
            CHECK (
                source_document_sha256 ~
                '^[0-9a-f]{64}$'
            ),

        CONSTRAINT workflow_source_not_empty
            CHECK (
                length(trim(source_name)) > 0
            ),

        CONSTRAINT workflow_phase_format
            CHECK (
                current_phase ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT workflow_status_format
            CHECK (
                current_status ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT workflow_lock_version
            CHECK (
                lock_version >= 1
            ),

        CONSTRAINT workflow_timestamp_order
            CHECK (
                updated_at >= created_at
                AND (
                    completed_at IS NULL
                    OR completed_at >= created_at
                )
            )
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.phase_result_references
    (
        reference_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        workflow_id UUID NOT NULL,
        phase_name TEXT NOT NULL,
        phase_version TEXT NOT NULL,
        attempt_number INTEGER NOT NULL DEFAULT 1,
        result_id TEXT NOT NULL,
        result_status TEXT NOT NULL,
        artifact_uri TEXT NOT NULL,
        artifact_sha256 CHAR(64) NOT NULL,
        review_required BOOLEAN NOT NULL
            DEFAULT FALSE,
        produced_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),
        metadata JSONB NOT NULL
            DEFAULT '{}'::jsonb,

        CONSTRAINT phase_reference_workflow_fk
            FOREIGN KEY (
                tenant_id,
                workflow_id
            )
            REFERENCES
                ap_agent.workflow_instances(
                    tenant_id,
                    workflow_id
                )
            ON DELETE RESTRICT,

        CONSTRAINT phase_reference_tenant_identity
            UNIQUE (
                tenant_id,
                reference_id
            ),

        CONSTRAINT phase_reference_attempt_identity
            UNIQUE (
                tenant_id,
                workflow_id,
                phase_name,
                phase_version,
                attempt_number
            ),

        CONSTRAINT phase_reference_name_format
            CHECK (
                phase_name ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT phase_reference_status_format
            CHECK (
                result_status ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT phase_reference_attempt_positive
            CHECK (
                attempt_number >= 1
            ),

        CONSTRAINT phase_reference_result_not_empty
            CHECK (
                length(trim(result_id)) > 0
            ),

        CONSTRAINT phase_reference_uri_not_empty
            CHECK (
                length(trim(artifact_uri)) > 0
            ),

        CONSTRAINT phase_reference_hash_format
            CHECK (
                artifact_sha256 ~
                '^[0-9a-f]{64}$'
            ),

        CONSTRAINT phase_reference_metadata_object
            CHECK (
                jsonb_typeof(metadata) = 'object'
            )
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.audit_events
    (
        event_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        workflow_id UUID NOT NULL,
        sequence_number BIGINT NOT NULL,
        event_type TEXT NOT NULL,
        phase_name TEXT NOT NULL,
        event_status TEXT NOT NULL,
        actor_type TEXT NOT NULL,
        actor_id TEXT,
        message TEXT NOT NULL,
        payload JSONB NOT NULL
            DEFAULT '{}'::jsonb,
        occurred_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),

        CONSTRAINT audit_workflow_fk
            FOREIGN KEY (
                tenant_id,
                workflow_id
            )
            REFERENCES
                ap_agent.workflow_instances(
                    tenant_id,
                    workflow_id
                )
            ON DELETE RESTRICT,

        CONSTRAINT audit_tenant_identity
            UNIQUE (
                tenant_id,
                event_id
            ),

        CONSTRAINT audit_sequence_identity
            UNIQUE (
                tenant_id,
                workflow_id,
                sequence_number
            ),

        CONSTRAINT audit_sequence_positive
            CHECK (
                sequence_number >= 1
            ),

        CONSTRAINT audit_event_type_format
            CHECK (
                event_type ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT audit_phase_format
            CHECK (
                phase_name ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT audit_status_format
            CHECK (
                event_status ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT audit_actor_type_format
            CHECK (
                actor_type ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT audit_message_not_empty
            CHECK (
                length(trim(message)) > 0
            ),

        CONSTRAINT audit_payload_object
            CHECK (
                jsonb_typeof(payload) = 'object'
            ),

        CONSTRAINT audit_timestamp_order
            CHECK (
                recorded_at >= occurred_at
            )
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.review_cases
    (
        review_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        workflow_id UUID NOT NULL,
        review_status TEXT NOT NULL DEFAULT 'OPEN',
        priority SMALLINT NOT NULL DEFAULT 3,
        reason_codes TEXT[] NOT NULL,
        summary TEXT NOT NULL,
        assigned_to TEXT,
        opened_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),
        due_at TIMESTAMPTZ,
        resolved_at TIMESTAMPTZ,
        resolution_code TEXT,
        resolution_notes TEXT,

        CONSTRAINT review_workflow_fk
            FOREIGN KEY (
                tenant_id,
                workflow_id
            )
            REFERENCES
                ap_agent.workflow_instances(
                    tenant_id,
                    workflow_id
                )
            ON DELETE RESTRICT,

        CONSTRAINT review_tenant_identity
            UNIQUE (
                tenant_id,
                review_id
            ),

        CONSTRAINT review_status_allowed
            CHECK (
                review_status IN (
                    'OPEN',
                    'CLAIMED',
                    'RESOLVED',
                    'CANCELLED'
                )
            ),

        CONSTRAINT review_priority_range
            CHECK (
                priority BETWEEN 1 AND 5
            ),

        CONSTRAINT review_reasons_present
            CHECK (
                cardinality(reason_codes) >= 1
            ),

        CONSTRAINT review_summary_not_empty
            CHECK (
                length(trim(summary)) > 0
            ),

        CONSTRAINT review_due_date_order
            CHECK (
                due_at IS NULL
                OR due_at >= opened_at
            ),

        CONSTRAINT review_resolution_state
            CHECK (
                (
                    review_status IN (
                        'OPEN',
                        'CLAIMED'
                    )
                    AND resolved_at IS NULL
                    AND resolution_code IS NULL
                )
                OR
                (
                    review_status IN (
                        'RESOLVED',
                        'CANCELLED'
                    )
                    AND resolved_at IS NOT NULL
                    AND resolution_code IS NOT NULL
                )
            ),

        CONSTRAINT review_resolution_time_order
            CHECK (
                resolved_at IS NULL
                OR resolved_at >= opened_at
            )
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.review_decisions
    (
        decision_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        workflow_id UUID NOT NULL,
        review_id UUID NOT NULL,
        decision_type TEXT NOT NULL,
        decided_by TEXT NOT NULL,
        decision_notes TEXT,
        evidence JSONB NOT NULL
            DEFAULT '{}'::jsonb,
        decided_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),

        CONSTRAINT decision_workflow_fk
            FOREIGN KEY (
                tenant_id,
                workflow_id
            )
            REFERENCES
                ap_agent.workflow_instances(
                    tenant_id,
                    workflow_id
                )
            ON DELETE RESTRICT,

        CONSTRAINT decision_review_fk
            FOREIGN KEY (
                tenant_id,
                review_id
            )
            REFERENCES
                ap_agent.review_cases(
                    tenant_id,
                    review_id
                )
            ON DELETE RESTRICT,

        CONSTRAINT decision_tenant_identity
            UNIQUE (
                tenant_id,
                decision_id
            ),

        CONSTRAINT decision_type_format
            CHECK (
                decision_type ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT decision_actor_not_empty
            CHECK (
                length(trim(decided_by)) > 0
            ),

        CONSTRAINT decision_evidence_object
            CHECK (
                jsonb_typeof(evidence) = 'object'
            ),

        CONSTRAINT decision_timestamp_order
            CHECK (
                recorded_at >= decided_at
            )
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX workflow_status_index
    ON ap_agent.workflow_instances
    (
        tenant_id,
        current_status,
        updated_at DESC
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX phase_reference_workflow_index
    ON ap_agent.phase_result_references
    (
        tenant_id,
        workflow_id,
        phase_name,
        attempt_number DESC
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX audit_event_timeline_index
    ON ap_agent.audit_events
    (
        tenant_id,
        workflow_id,
        sequence_number
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX review_open_queue_index
    ON ap_agent.review_cases
    (
        tenant_id,
        priority,
        opened_at
    )
    WHERE review_status IN (
        'OPEN',
        'CLAIMED'
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX review_decision_timeline_index
    ON ap_agent.review_decisions
    (
        tenant_id,
        review_id,
        decided_at
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE FUNCTION
        ap_agent.reject_append_only_mutation()
    RETURNS TRIGGER
    LANGUAGE plpgsql
    AS $$
    BEGIN
        RAISE EXCEPTION
            'Table %.% is append-only; % is prohibited.',
            TG_TABLE_SCHEMA,
            TG_TABLE_NAME,
            TG_OP
        USING ERRCODE = '55000';
    END;
    $$;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER audit_events_no_mutation
    BEFORE UPDATE OR DELETE
    ON ap_agent.audit_events
    FOR EACH ROW
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE
    ON ap_agent.audit_events
    FOR EACH STATEMENT
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER review_decisions_no_mutation
    BEFORE UPDATE OR DELETE
    ON ap_agent.review_decisions
    FOR EACH ROW
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER review_decisions_no_truncate
    BEFORE TRUNCATE
    ON ap_agent.review_decisions
    FOR EACH STATEMENT
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER TABLE ap_agent.tenants
        ENABLE ROW LEVEL SECURITY;
    ALTER TABLE ap_agent.tenants
        FORCE ROW LEVEL SECURITY;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE POLICY tenant_isolation
    ON ap_agent.tenants
    USING (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    )
    WITH CHECK (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER TABLE ap_agent.workflow_instances
        ENABLE ROW LEVEL SECURITY;
    ALTER TABLE ap_agent.workflow_instances
        FORCE ROW LEVEL SECURITY;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE POLICY tenant_isolation
    ON ap_agent.workflow_instances
    USING (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    )
    WITH CHECK (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER TABLE ap_agent.phase_result_references
        ENABLE ROW LEVEL SECURITY;
    ALTER TABLE ap_agent.phase_result_references
        FORCE ROW LEVEL SECURITY;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE POLICY tenant_isolation
    ON ap_agent.phase_result_references
    USING (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    )
    WITH CHECK (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER TABLE ap_agent.audit_events
        ENABLE ROW LEVEL SECURITY;
    ALTER TABLE ap_agent.audit_events
        FORCE ROW LEVEL SECURITY;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE POLICY tenant_isolation
    ON ap_agent.audit_events
    USING (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    )
    WITH CHECK (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER TABLE ap_agent.review_cases
        ENABLE ROW LEVEL SECURITY;
    ALTER TABLE ap_agent.review_cases
        FORCE ROW LEVEL SECURITY;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE POLICY tenant_isolation
    ON ap_agent.review_cases
    USING (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    )
    WITH CHECK (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER TABLE ap_agent.review_decisions
        ENABLE ROW LEVEL SECURITY;
    ALTER TABLE ap_agent.review_decisions
        FORCE ROW LEVEL SECURITY;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE POLICY tenant_isolation
    ON ap_agent.review_decisions
    USING (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    )
    WITH CHECK (
        tenant_id = NULLIF(
            current_setting(
                'ap_agent.tenant_id',
                TRUE
            ),
            ''
        )::UUID
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    REVOKE ALL
    ON SCHEMA ap_agent
    FROM PUBLIC;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    REVOKE ALL
    ON ALL TABLES
    IN SCHEMA ap_agent
    FROM PUBLIC;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER DEFAULT PRIVILEGES
    IN SCHEMA ap_agent
    REVOKE ALL
    ON TABLES
    FROM PUBLIC;
    