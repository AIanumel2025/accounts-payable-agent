import { StatusBadge } from "@/components/ui/StatusBadge";
import {
  formatReviewReason,
  formatShortIdentifier,
  reviewCaseStatusLabel,
  stageLabel,
  workflowStatusLabel,
} from "@/lib/formatting";
import type { InvoiceDetailPayload } from "@/types/api-payloads";
import styles from "./DetailSections.module.css";

/** Section A (M11B task §8A): identity and status. */
export function IdentitySection({ detail }: { detail: InvoiceDetailPayload }) {
  const reviewCaseId = formatShortIdentifier(detail.review_case_id);
  const workflowId = formatShortIdentifier(detail.workflow_id);
  const batchId = formatShortIdentifier(detail.batch_id);
  const documentId = formatShortIdentifier(detail.document_id);

  return (
    <section aria-labelledby="section-identity" className={styles.section}>
      <h2 id="section-identity" className={styles.sectionTitle}>
        Identity and status
      </h2>
      <dl className={styles.identityGrid}>
        <div>
          <dt>Source document</dt>
          <dd>{detail.source_name}</dd>
        </div>
        <div>
          <dt>Workflow status</dt>
          <dd>
            <StatusBadge presentation={workflowStatusLabel(detail.workflow_status)} />
          </dd>
        </div>
        <div>
          <dt>Current stage</dt>
          <dd>
            <StatusBadge presentation={stageLabel(detail.current_stage)} />
          </dd>
        </div>
        <div>
          <dt>Review case status</dt>
          <dd>
            <StatusBadge presentation={reviewCaseStatusLabel(detail.case_status)} />
          </dd>
        </div>
        <div>
          <dt>Review required</dt>
          <dd>{detail.review_required ? "Yes" : "No"}</dd>
        </div>
        <div>
          <dt>Review revision</dt>
          <dd>{detail.review_revision}</dd>
        </div>
        <div>
          <dt>Review case ID</dt>
          <dd className={styles.mono} title={reviewCaseId.full}>
            {reviewCaseId.display}
          </dd>
        </div>
        <div>
          <dt>Workflow ID</dt>
          <dd className={styles.mono} title={workflowId.full}>
            {workflowId.display}
          </dd>
        </div>
        <div>
          <dt>Batch ID</dt>
          <dd className={styles.mono} title={batchId.full}>
            {batchId.display}
          </dd>
        </div>
        <div>
          <dt>Document ID</dt>
          <dd className={styles.mono} title={documentId.full}>
            {documentId.display}
          </dd>
        </div>
      </dl>
      {detail.review_reasons.length > 0 ? (
        <div>
          <p className={styles.sectionNote}>Review reasons</p>
          <ul className={styles.reasonList}>
            {detail.review_reasons.map((reason) => (
              <li key={reason}>{formatReviewReason(reason)}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
