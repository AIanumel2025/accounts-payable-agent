import { EmptyState } from "@/components/feedback/EmptyState";
import { fieldNameLabel, humanizeCode } from "@/lib/formatting";
import type { FinancialCheckPayload, InterfaceFieldValuePayload } from "@/types/api-payloads";
import styles from "./DetailSections.module.css";

interface EvidenceRow {
  sourceLabel: string;
  referenceIds: readonly string[];
}

/**
 * Section C (M11B task §8C/§9/§21): evidence references. This is a
 * read-only, non-resolving view of the opaque evidence-reference IDs the
 * backend already returns per field/check (`InterfaceFieldValueResponse.
 * evidence_reference_ids`, `FinancialCheckResponse.evidence_reference_ids`
 * -- `src/ap_agent/api/schemas.py`) -- the API contract has no separate
 * evidence-lookup endpoint and no document-streaming/signed-URL mechanism
 * in M11B, so an ID is rendered as an opaque token, never resolved to a
 * local path, credential, or URL. `original_document_uri` stays rendered
 * as unavailable for as long as it is `null`, per the backend's own
 * comment on `InvoiceDetailResponse.from_domain` that it will only be
 * populated once a secure authenticated streaming/signed-URL mechanism
 * exists.
 */
export function EvidenceSection({
  sourceDocumentSha256,
  originalDocumentUri,
  fields,
  financialChecks,
}: {
  sourceDocumentSha256: string;
  originalDocumentUri: string | null;
  fields: InterfaceFieldValuePayload[];
  financialChecks: FinancialCheckPayload[];
}) {
  const fieldRows: EvidenceRow[] = fields
    .filter((fieldValue) => fieldValue.evidence_reference_ids.length > 0)
    .map((fieldValue) => ({
      sourceLabel: fieldNameLabel(fieldValue.field_name),
      referenceIds: fieldValue.evidence_reference_ids,
    }));

  const checkRows: EvidenceRow[] = financialChecks
    .filter((check) => check.evidence_reference_ids.length > 0)
    .map((check) => ({
      sourceLabel: `Financial check: ${humanizeCode(check.check_type)}`,
      referenceIds: check.evidence_reference_ids,
    }));

  const allRows = [...fieldRows, ...checkRows];
  const totalReferences = allRows.reduce((sum, row) => sum + row.referenceIds.length, 0);

  return (
    <section aria-labelledby="section-evidence" className={styles.section}>
      <h2 id="section-evidence" className={styles.sectionTitle}>
        Evidence references
      </h2>
      <dl className={styles.identityGrid}>
        <div>
          <dt>Document checksum (SHA-256)</dt>
          <dd className={styles.mono}>{sourceDocumentSha256}</dd>
        </div>
        <div>
          <dt>Original document</dt>
          <dd className={styles.muted}>
            {originalDocumentUri ?? "Not available (document viewing is not part of this milestone)"}
          </dd>
        </div>
        <div>
          <dt>Total evidence references</dt>
          <dd>{totalReferences}</dd>
        </div>
      </dl>
      <p className={styles.sectionNote}>
        Evidence references are opaque identifiers recorded by extraction and validation. This milestone does not
        stream, link to, or preview the underlying document -- see the review-case detail report for the full
        evidence-access decision.
      </p>
      {allRows.length === 0 ? (
        <EmptyState
          title="No evidence references recorded"
          description="No field or financial check on this document has an associated evidence reference."
        />
      ) : (
        <ul className={styles.decisionList}>
          {allRows.map((row) => (
            <li key={row.sourceLabel} className={styles.decisionCard}>
              <strong>{row.sourceLabel}</strong>
              <ul className={styles.evidenceList}>
                {row.referenceIds.map((referenceId) => (
                  <li key={referenceId}>{referenceId}</li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
