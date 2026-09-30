-- 0004_durable_operations
-- M11D Core (operations console and controlled workflow execution): a simple
-- tenant-isolated job queue for a single worker, append-only job events and
-- append-only derived invoice-memory versions. No leases, heartbeats, retries
-- or dead-letter state (deferred to M11E).
-- Additive only: no earlier migration, table, trigger or policy is edited.
-- Checksum is computed over each '-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===' -delimited
-- statement block, stripped and newline-joined (same algorithm as 0001-0003).
-- Do not reformat statement bodies: doing so changes the checksum recorded
-- for an already-applied migration.

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.invoice_memory_versions
    (
        version_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        workflow_id UUID NOT NULL,
        batch_id UUID NOT NULL,
        document_id UUID NOT NULL,
        parent_memory_record_id UUID NOT NULL,
        parent_memory_version_id UUID,
        resume_plan_id UUID NOT NULL,
        decision_id UUID NOT NULL,
        derived_version TEXT NOT NULL,
        restart_stage TEXT NOT NULL,
        source_document_sha256 CHAR(64) NOT NULL,
        source_memory_payload_sha256 CHAR(64) NOT NULL,
        source_normalization_sha256 CHAR(64) NOT NULL,
        correction_overlay_sha256 CHAR(64) NOT NULL,
        invoice_number TEXT,
        supplier_name TEXT,
        currency TEXT,
        total_amount NUMERIC,
        normalization_status TEXT NOT NULL,
        financial_validation_status TEXT NOT NULL,
        matching_status TEXT NOT NULL,
        supplier_resolution_status TEXT NOT NULL,
        purchase_order_status TEXT NOT NULL,
        review_required BOOLEAN NOT NULL,
        review_reasons TEXT[] NOT NULL
            DEFAULT ARRAY[]::TEXT[],
        terminal_status TEXT NOT NULL,
        new_review_id UUID,
        normalized_invoice JSONB NOT NULL,
        financial_validation JSONB NOT NULL,
        matching_result JSONB NOT NULL,
        matched_reference_data JSONB NOT NULL,
        payload_sha256 CHAR(64) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),

        CONSTRAINT memory_version_workflow_fk
            FOREIGN KEY (tenant_id, workflow_id)
            REFERENCES ap_agent.workflow_instances(tenant_id, workflow_id)
            ON DELETE RESTRICT,

        CONSTRAINT memory_version_parent_record_fk
            FOREIGN KEY (tenant_id, parent_memory_record_id)
            REFERENCES ap_agent.invoice_memory_records(tenant_id, memory_record_id)
            ON DELETE RESTRICT,

        CONSTRAINT memory_version_decision_fk
            FOREIGN KEY (tenant_id, decision_id)
            REFERENCES ap_agent.review_decisions(tenant_id, decision_id)
            ON DELETE RESTRICT,

        CONSTRAINT memory_version_tenant_identity
            UNIQUE (tenant_id, version_id),

        CONSTRAINT memory_version_one_per_plan
            UNIQUE (tenant_id, resume_plan_id),

        CONSTRAINT memory_version_parent_version_fk
            FOREIGN KEY (tenant_id, parent_memory_version_id)
            REFERENCES ap_agent.invoice_memory_versions(tenant_id, version_id)
            ON DELETE RESTRICT,

        CONSTRAINT memory_version_restart_stage_allowed
            CHECK (
                restart_stage IN (
                    'FINANCIAL_VALIDATION',
                    'REFERENCE_MATCHING',
                    'MEMORY_PERSISTENCE'
                )
            ),

        CONSTRAINT memory_version_terminal_status_allowed
            CHECK (terminal_status IN ('SUCCEEDED', 'REVIEW_REQUIRED')),

        CONSTRAINT memory_version_review_state_consistent
            CHECK (
                review_required = (terminal_status = 'REVIEW_REQUIRED')
                AND (new_review_id IS NOT NULL) = review_required
            ),

        CONSTRAINT memory_version_hash_format
            CHECK (
                source_document_sha256 ~ '^[0-9a-f]{64}$'
                AND source_memory_payload_sha256 ~ '^[0-9a-f]{64}$'
                AND source_normalization_sha256 ~ '^[0-9a-f]{64}$'
                AND correction_overlay_sha256 ~ '^[0-9a-f]{64}$'
                AND payload_sha256 ~ '^[0-9a-f]{64}$'
            ),

        CONSTRAINT memory_version_payload_objects
            CHECK (
                jsonb_typeof(normalized_invoice) = 'object'
                AND jsonb_typeof(financial_validation) = 'object'
                AND jsonb_typeof(matching_result) = 'object'
                AND jsonb_typeof(matched_reference_data) = 'object'
            )
    );

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER TABLE ap_agent.review_cases
        ADD COLUMN memory_version_id UUID,
        ADD COLUMN source_resume_plan_id UUID,
        ADD CONSTRAINT review_case_memory_version_fk
            FOREIGN KEY (tenant_id, memory_version_id)
            REFERENCES ap_agent.invoice_memory_versions(tenant_id, version_id)
            ON DELETE RESTRICT,
        ADD CONSTRAINT review_case_version_link_together
            CHECK ((memory_version_id IS NULL) = (source_resume_plan_id IS NULL));

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE FUNCTION
        ap_agent.reject_review_case_link_change()
    RETURNS TRIGGER
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF NEW.memory_version_id IS DISTINCT FROM OLD.memory_version_id
           OR NEW.source_resume_plan_id IS DISTINCT FROM OLD.source_resume_plan_id THEN
            RAISE EXCEPTION
                'review_cases version link columns are immutable.'
            USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END;
    $$;

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER review_cases_version_link_immutable
    BEFORE UPDATE
    ON ap_agent.review_cases
    FOR EACH ROW
    EXECUTE FUNCTION
        ap_agent.reject_review_case_link_change();

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.workflow_jobs
    (
        job_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        job_type TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_fingerprint CHAR(64) NOT NULL,
        source_name TEXT NOT NULL,
        batch_id UUID,
        workflow_id UUID,
        document_id UUID,
        review_id UUID,
        resume_plan_id UUID,
        artifact_uri TEXT,
        artifact_sha256 CHAR(64),
        media_type TEXT,
        byte_size BIGINT,
        status TEXT NOT NULL DEFAULT 'QUEUED',
        current_stage TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 1,
        error_code TEXT,
        result_summary JSONB NOT NULL
            DEFAULT '{}'::jsonb,
        result_review_id UUID,
        result_version_id UUID,
        created_by TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        lock_version BIGINT NOT NULL DEFAULT 1,

        CONSTRAINT job_tenant_fk
            FOREIGN KEY (tenant_id)
            REFERENCES ap_agent.tenants(tenant_id)
            ON DELETE RESTRICT,

        CONSTRAINT job_workflow_fk
            FOREIGN KEY (tenant_id, workflow_id)
            REFERENCES ap_agent.workflow_instances(tenant_id, workflow_id)
            ON DELETE RESTRICT,

        CONSTRAINT job_review_fk
            FOREIGN KEY (tenant_id, review_id)
            REFERENCES ap_agent.review_cases(tenant_id, review_id)
            ON DELETE RESTRICT,

        CONSTRAINT job_result_review_fk
            FOREIGN KEY (tenant_id, result_review_id)
            REFERENCES ap_agent.review_cases(tenant_id, review_id)
            ON DELETE RESTRICT,

        CONSTRAINT job_result_version_fk
            FOREIGN KEY (tenant_id, result_version_id)
            REFERENCES ap_agent.invoice_memory_versions(tenant_id, version_id)
            ON DELETE RESTRICT,

        CONSTRAINT job_tenant_identity
            UNIQUE (tenant_id, job_id),

        CONSTRAINT job_idempotency_identity
            UNIQUE (tenant_id, job_type, idempotency_key),

        CONSTRAINT job_one_per_resume_plan
            UNIQUE (tenant_id, resume_plan_id),

        CONSTRAINT job_type_allowed
            CHECK (job_type IN ('PROCESS_DOCUMENT', 'RESUME_WORKFLOW')),

        CONSTRAINT job_status_allowed
            CHECK (
                status IN (
                    'QUEUED',
                    'RUNNING',
                    'SUCCEEDED',
                    'REVIEW_REQUIRED',
                    'FAILED'
                )
            ),

        CONSTRAINT job_idempotency_key_format
            CHECK (idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$'),

        CONSTRAINT job_fingerprint_format
            CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),

        CONSTRAINT job_source_name_not_empty
            CHECK (
                length(trim(source_name)) > 0
                AND length(source_name) <= 256
            ),

        CONSTRAINT job_payload_shape
            CHECK (
                (
                    job_type = 'PROCESS_DOCUMENT'
                    AND artifact_uri IS NOT NULL
                    AND artifact_sha256 IS NOT NULL
                    AND media_type IS NOT NULL
                    AND byte_size IS NOT NULL
                    AND resume_plan_id IS NULL
                )
                OR
                (
                    job_type = 'RESUME_WORKFLOW'
                    AND resume_plan_id IS NOT NULL
                    AND workflow_id IS NOT NULL
                    AND review_id IS NOT NULL
                    AND artifact_uri IS NULL
                    AND artifact_sha256 IS NULL
                )
            ),

        CONSTRAINT job_artifact_hash_format
            CHECK (
                artifact_sha256 IS NULL
                OR artifact_sha256 ~ '^[0-9a-f]{64}$'
            ),

        CONSTRAINT job_byte_size_positive
            CHECK (byte_size IS NULL OR byte_size > 0),

        CONSTRAINT job_attempt_bounds
            CHECK (max_attempts = 1 AND attempt_count BETWEEN 0 AND 1),

        CONSTRAINT job_terminal_completed_at
            CHECK (
                (status IN ('SUCCEEDED', 'REVIEW_REQUIRED', 'FAILED'))
                = (completed_at IS NOT NULL)
            ),

        CONSTRAINT job_failure_has_error_code
            CHECK (
                status <> 'FAILED'
                OR error_code IS NOT NULL
            ),

        CONSTRAINT job_error_code_format
            CHECK (
                error_code IS NULL
                OR error_code ~ '^[A-Z][A-Z0-9_]{2,63}$'
            ),

        CONSTRAINT job_stage_format
            CHECK (
                current_stage IS NULL
                OR current_stage ~ '^[A-Z][A-Z0-9_]*$'
            ),

        CONSTRAINT job_summary_object
            CHECK (jsonb_typeof(result_summary) = 'object'),

        CONSTRAINT job_lock_version_positive
            CHECK (lock_version >= 1),

        CONSTRAINT job_created_by_not_empty
            CHECK (length(trim(created_by)) > 0)
    );

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TABLE ap_agent.workflow_job_events
    (
        event_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        job_id UUID NOT NULL,
        sequence_number BIGINT NOT NULL,
        event_type TEXT NOT NULL,
        stage TEXT,
        status TEXT NOT NULL,
        attempt_number INTEGER NOT NULL DEFAULT 0,
        message TEXT NOT NULL,
        error_code TEXT,
        occurred_at TIMESTAMPTZ NOT NULL
            DEFAULT transaction_timestamp(),

        CONSTRAINT job_event_job_fk
            FOREIGN KEY (tenant_id, job_id)
            REFERENCES ap_agent.workflow_jobs(tenant_id, job_id)
            ON DELETE RESTRICT,

        CONSTRAINT job_event_sequence_identity
            UNIQUE (tenant_id, job_id, sequence_number),

        CONSTRAINT job_event_sequence_positive
            CHECK (sequence_number >= 1),

        CONSTRAINT job_event_type_format
            CHECK (event_type ~ '^[A-Z][A-Z0-9_]*$'),

        CONSTRAINT job_event_stage_format
            CHECK (stage IS NULL OR stage ~ '^[A-Z][A-Z0-9_]*$'),

        CONSTRAINT job_event_status_format
            CHECK (status ~ '^[A-Z][A-Z0-9_]*$'),

        CONSTRAINT job_event_attempt_non_negative
            CHECK (attempt_number >= 0),

        CONSTRAINT job_event_message_bounded
            CHECK (
                length(trim(message)) > 0
                AND length(message) <= 500
            ),

        CONSTRAINT job_event_error_code_format
            CHECK (
                error_code IS NULL
                OR error_code ~ '^[A-Z][A-Z0-9_]{2,63}$'
            )
    );

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX workflow_jobs_claim_index
    ON ap_agent.workflow_jobs
    (
        created_at
    )
    WHERE status = 'QUEUED';

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX workflow_jobs_tenant_listing_index
    ON ap_agent.workflow_jobs
    (
        tenant_id,
        created_at DESC,
        job_id
    );

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX workflow_jobs_workflow_index
    ON ap_agent.workflow_jobs
    (
        tenant_id,
        workflow_id
    )
    WHERE workflow_id IS NOT NULL;

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX workflow_job_events_timeline_index
    ON ap_agent.workflow_job_events
    (
        tenant_id,
        job_id,
        sequence_number
    );

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE INDEX memory_versions_workflow_index
    ON ap_agent.invoice_memory_versions
    (
        tenant_id,
        workflow_id,
        created_at DESC
    );

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE FUNCTION
        ap_agent.guard_workflow_job_transition()
    RETURNS TRIGGER
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF NEW.job_id IS DISTINCT FROM OLD.job_id
           OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.job_type IS DISTINCT FROM OLD.job_type
           OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
           OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
           OR NEW.source_name IS DISTINCT FROM OLD.source_name
           OR NEW.artifact_uri IS DISTINCT FROM OLD.artifact_uri
           OR NEW.artifact_sha256 IS DISTINCT FROM OLD.artifact_sha256
           OR NEW.media_type IS DISTINCT FROM OLD.media_type
           OR NEW.byte_size IS DISTINCT FROM OLD.byte_size
           OR NEW.resume_plan_id IS DISTINCT FROM OLD.resume_plan_id
           OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
           OR NEW.created_by IS DISTINCT FROM OLD.created_by
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION
                'workflow_jobs identity columns are immutable.'
            USING ERRCODE = '55000';
        END IF;

        IF OLD.status IN ('SUCCEEDED', 'REVIEW_REQUIRED', 'FAILED') THEN
            RAISE EXCEPTION
                'workflow_jobs terminal state % is final.', OLD.status
            USING ERRCODE = '55000';
        END IF;

        IF NOT (
            (OLD.status = 'QUEUED' AND NEW.status IN ('QUEUED', 'RUNNING'))
            OR (
                OLD.status = 'RUNNING'
                AND NEW.status IN ('RUNNING', 'SUCCEEDED', 'REVIEW_REQUIRED', 'FAILED')
            )
        ) THEN
            RAISE EXCEPTION
                'workflow_jobs transition % -> % is not allowed.', OLD.status, NEW.status
            USING ERRCODE = '55000';
        END IF;

        IF NEW.lock_version IS DISTINCT FROM OLD.lock_version + 1 THEN
            RAISE EXCEPTION
                'workflow_jobs updates must advance lock_version by exactly one.'
            USING ERRCODE = '55000';
        END IF;

        IF NEW.attempt_count < OLD.attempt_count THEN
            RAISE EXCEPTION
                'workflow_jobs attempt_count must not decrease.'
            USING ERRCODE = '55000';
        END IF;

        RETURN NEW;
    END;
    $$;

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER workflow_jobs_guard_transition
    BEFORE UPDATE
    ON ap_agent.workflow_jobs
    FOR EACH ROW
    EXECUTE FUNCTION
        ap_agent.guard_workflow_job_transition();

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER workflow_jobs_no_delete
    BEFORE DELETE
    ON ap_agent.workflow_jobs
    FOR EACH ROW
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER workflow_jobs_no_truncate
    BEFORE TRUNCATE
    ON ap_agent.workflow_jobs
    FOR EACH STATEMENT
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER workflow_job_events_no_mutation
    BEFORE UPDATE OR DELETE
    ON ap_agent.workflow_job_events
    FOR EACH ROW
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER workflow_job_events_no_truncate
    BEFORE TRUNCATE
    ON ap_agent.workflow_job_events
    FOR EACH STATEMENT
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER invoice_memory_versions_no_mutation
    BEFORE UPDATE OR DELETE
    ON ap_agent.invoice_memory_versions
    FOR EACH ROW
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE TRIGGER invoice_memory_versions_no_truncate
    BEFORE TRUNCATE
    ON ap_agent.invoice_memory_versions
    FOR EACH STATEMENT
    EXECUTE FUNCTION
        ap_agent.reject_append_only_mutation();

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER TABLE ap_agent.invoice_memory_versions
        ENABLE ROW LEVEL SECURITY;
    ALTER TABLE ap_agent.invoice_memory_versions
        FORCE ROW LEVEL SECURITY;

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE POLICY tenant_isolation
    ON ap_agent.invoice_memory_versions
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

    ALTER TABLE ap_agent.workflow_jobs
        ENABLE ROW LEVEL SECURITY;
    ALTER TABLE ap_agent.workflow_jobs
        FORCE ROW LEVEL SECURITY;

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE POLICY tenant_isolation
    ON ap_agent.workflow_jobs
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

    CREATE POLICY worker_queue_select
    ON ap_agent.workflow_jobs
    FOR SELECT
    USING (
        current_setting(
            'ap_agent.worker_scope',
            TRUE
        ) = 'queue'
    );

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE POLICY worker_queue_update
    ON ap_agent.workflow_jobs
    FOR UPDATE
    USING (
        current_setting(
            'ap_agent.worker_scope',
            TRUE
        ) = 'queue'
    )
    WITH CHECK (
        current_setting(
            'ap_agent.worker_scope',
            TRUE
        ) = 'queue'
    );

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    ALTER TABLE ap_agent.workflow_job_events
        ENABLE ROW LEVEL SECURITY;
    ALTER TABLE ap_agent.workflow_job_events
        FORCE ROW LEVEL SECURITY;

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE POLICY tenant_isolation
    ON ap_agent.workflow_job_events
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

    CREATE VIEW ap_agent.review_case_effective_memory
    WITH (security_invoker = true)
    AS
    SELECT
        review.tenant_id AS tenant_id,
        review.review_id AS review_id,
        review.workflow_id AS workflow_id,
        original.memory_record_id AS original_memory_record_id,
        version.version_id AS memory_version_id,
        COALESCE(version.derived_version, 'original') AS memory_version_label,
        COALESCE(version.invoice_number, original.invoice_number) AS invoice_number,
        COALESCE(version.supplier_name, original.supplier_name) AS supplier_name,
        COALESCE(version.currency, original.currency) AS currency,
        COALESCE(version.total_amount, original.total_amount) AS total_amount,
        COALESCE(version.supplier_resolution_status, original.supplier_resolution_status)
            AS supplier_resolution_status,
        COALESCE(version.purchase_order_status, original.purchase_order_status)
            AS purchase_order_status,
        COALESCE(version.financial_validation_status, original.financial_validation_status)
            AS financial_validation_status,
        COALESCE(version.review_required, original.review_required) AS review_required,
        COALESCE(version.review_reasons, original.review_reasons) AS review_reasons,
        COALESCE(version.payload_sha256, original.payload_sha256) AS payload_sha256,
        COALESCE(version.normalized_invoice, original.normalized_invoice) AS normalized_invoice,
        COALESCE(version.financial_validation, original.financial_validation)
            AS financial_validation,
        COALESCE(version.matching_result, original.matching_result) AS matching_result,
        COALESCE(version.matched_reference_data, original.matched_reference_data)
            AS matched_reference_data
    FROM ap_agent.review_cases AS review
    JOIN ap_agent.invoice_memory_records AS original
        ON original.tenant_id = review.tenant_id
       AND original.workflow_id = review.workflow_id
    LEFT JOIN ap_agent.invoice_memory_versions AS version
        ON version.tenant_id = review.tenant_id
       AND version.version_id = review.memory_version_id;

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    CREATE VIEW ap_agent.workflow_effective_memory
    WITH (security_invoker = true)
    AS
    SELECT
        original.tenant_id AS tenant_id,
        original.workflow_id AS workflow_id,
        original.memory_record_id AS original_memory_record_id,
        latest.version_id AS memory_version_id,
        COALESCE(latest.derived_version, 'original') AS memory_version_label,
        COALESCE(latest.invoice_number, original.invoice_number) AS invoice_number,
        COALESCE(latest.supplier_name, original.supplier_name) AS supplier_name,
        COALESCE(latest.currency, original.currency) AS currency,
        COALESCE(latest.total_amount, original.total_amount) AS total_amount,
        COALESCE(latest.supplier_resolution_status, original.supplier_resolution_status)
            AS supplier_resolution_status,
        COALESCE(latest.purchase_order_status, original.purchase_order_status)
            AS purchase_order_status,
        COALESCE(latest.financial_validation_status, original.financial_validation_status)
            AS financial_validation_status,
        COALESCE(latest.review_required, original.review_required) AS review_required,
        COALESCE(latest.review_reasons, original.review_reasons) AS review_reasons,
        COALESCE(latest.payload_sha256, original.payload_sha256) AS payload_sha256,
        COALESCE(latest.normalized_invoice, original.normalized_invoice) AS normalized_invoice,
        COALESCE(latest.financial_validation, original.financial_validation)
            AS financial_validation,
        COALESCE(latest.matching_result, original.matching_result) AS matching_result,
        COALESCE(latest.matched_reference_data, original.matched_reference_data)
            AS matched_reference_data
    FROM ap_agent.invoice_memory_records AS original
    LEFT JOIN LATERAL (
        SELECT candidate.*
        FROM ap_agent.invoice_memory_versions AS candidate
        WHERE candidate.tenant_id = original.tenant_id
          AND candidate.workflow_id = original.workflow_id
        ORDER BY candidate.created_at DESC, candidate.version_id DESC
        LIMIT 1
    ) AS latest ON TRUE;

-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===

    REVOKE ALL
    ON ap_agent.invoice_memory_versions,
       ap_agent.workflow_jobs,
       ap_agent.workflow_job_events,
       ap_agent.review_case_effective_memory,
       ap_agent.workflow_effective_memory
    FROM PUBLIC;
