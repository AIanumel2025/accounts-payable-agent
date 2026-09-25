-- 0003_matched_invoice_memory
-- Source: notebook Phase 7 Replacement Cell 5 (INVOICE_MEMORY_MIGRATION_STATEMENTS), verbatim.
-- Checksum is computed over each '-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===' -delimited
-- statement block, stripped and newline-joined, exactly as the notebook's
-- canonicalize_migration() does. Do not reformat statement bodies: doing
-- so changes the checksum recorded for an already-applied migration.

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.invoice_memory_records
    (
        memory_record_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        workflow_id UUID NOT NULL,
        batch_id UUID NOT NULL,
        document_id UUID NOT NULL,
        matching_result_id UUID NOT NULL,

        source_name TEXT NOT NULL,
        source_document_sha256 CHAR(64) NOT NULL,

        invoice_record_id UUID NOT NULL,
        invoice_number TEXT,
        supplier_name TEXT,
        currency TEXT,
        total_amount NUMERIC,

        supplier_resolution_status TEXT NOT NULL,
        matched_supplier_id TEXT,

        purchase_order_status TEXT NOT NULL,
        purchase_order_id TEXT,
        purchase_order_number TEXT,

        goods_receipt_status TEXT NOT NULL,
        goods_receipt_ids TEXT[] NOT NULL
            DEFAULT ARRAY[]::TEXT[],

        match_mode TEXT NOT NULL,
        line_match_count INTEGER NOT NULL,

        normalization_status TEXT NOT NULL,
        financial_validation_status TEXT NOT NULL,
        matching_status TEXT NOT NULL,

        review_required BOOLEAN NOT NULL,
        review_reasons TEXT[] NOT NULL
            DEFAULT ARRAY[]::TEXT[],

        normalized_invoice JSONB NOT NULL,
        financial_validation JSONB NOT NULL,
        matching_result JSONB NOT NULL,
        matched_reference_data JSONB NOT NULL,

        payload_sha256 CHAR(64) NOT NULL,
        stored_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),

        CONSTRAINT invoice_memory_workflow_fk
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

        CONSTRAINT invoice_memory_tenant_identity
            UNIQUE (
                tenant_id,
                memory_record_id
            ),

        CONSTRAINT invoice_memory_document_identity
            UNIQUE (
                tenant_id,
                document_id
            ),

        CONSTRAINT invoice_memory_matching_identity
            UNIQUE (
                tenant_id,
                matching_result_id
            ),

        CONSTRAINT invoice_memory_source_not_empty
            CHECK (
                length(trim(source_name)) > 0
            ),

        CONSTRAINT invoice_memory_source_hash
            CHECK (
                source_document_sha256 ~
                '^[0-9a-f]{64}$'
            ),

        CONSTRAINT invoice_memory_payload_hash
            CHECK (
                payload_sha256 ~
                '^[0-9a-f]{64}$'
            ),

        CONSTRAINT invoice_memory_line_count
            CHECK (
                line_match_count >= 0
            ),

        CONSTRAINT invoice_memory_supplier_status
            CHECK (
                supplier_resolution_status ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT invoice_memory_po_status
            CHECK (
                purchase_order_status ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT invoice_memory_gr_status
            CHECK (
                goods_receipt_status ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT invoice_memory_match_mode
            CHECK (
                match_mode ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT invoice_memory_normalization_status
            CHECK (
                normalization_status ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT invoice_memory_validation_status
            CHECK (
                financial_validation_status ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT invoice_memory_matching_status
            CHECK (
                matching_status ~
                '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT invoice_memory_normalized_object
            CHECK (
                jsonb_typeof(normalized_invoice)
                = 'object'
            ),

        CONSTRAINT invoice_memory_validation_object
            CHECK (
                jsonb_typeof(financial_validation)
                = 'object'
            ),

        CONSTRAINT invoice_memory_matching_object
            CHECK (
                jsonb_typeof(matching_result)
                = 'object'
            ),

        CONSTRAINT invoice_memory_reference_object
            CHECK (
                jsonb_typeof(matched_reference_data)
                = 'object'
            )
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX invoice_memory_invoice_number_index
    ON ap_agent.invoice_memory_records
    (
        tenant_id,
        invoice_number
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX invoice_memory_supplier_index
    ON ap_agent.invoice_memory_records
    (
        tenant_id,
        matched_supplier_id
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX invoice_memory_po_index
    ON ap_agent.invoice_memory_records
    (
        tenant_id,
        purchase_order_number
    );
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX invoice_memory_review_index
    ON ap_agent.invoice_memory_records
    (
        tenant_id,
        matching_status,
        stored_at DESC
    )
    WHERE review_required = TRUE;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER invoice_memory_no_mutation
    BEFORE UPDATE OR DELETE
    ON ap_agent.invoice_memory_records
    FOR EACH ROW
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER invoice_memory_no_truncate
    BEFORE TRUNCATE
    ON ap_agent.invoice_memory_records
    FOR EACH STATEMENT
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER TABLE ap_agent.invoice_memory_records
        ENABLE ROW LEVEL SECURITY;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER TABLE ap_agent.invoice_memory_records
        FORCE ROW LEVEL SECURITY;
    
-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE POLICY tenant_isolation
    ON ap_agent.invoice_memory_records
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
    ON ap_agent.invoice_memory_records
    FROM PUBLIC;
    