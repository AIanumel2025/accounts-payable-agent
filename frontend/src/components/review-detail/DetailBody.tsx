import { EvidenceSection } from "@/components/review-detail/EvidenceSection";
import { FieldsSection } from "@/components/review-detail/FieldsSection";
import { FinancialChecksSection } from "@/components/review-detail/FinancialChecksSection";
import { IdentitySection } from "@/components/review-detail/IdentitySection";
import { LineMatchesSection } from "@/components/review-detail/LineMatchesSection";
import { MatchingSection } from "@/components/review-detail/MatchingSection";
import { ReviewDecisionsSection } from "@/components/review-detail/ReviewDecisionsSection";
import { TimelineSection } from "@/components/review-detail/TimelineSection";
import type { InvoiceDetailPayload } from "@/types/api-payloads";
import styles from "./DetailBody.module.css";

/**
 * The invoice/review-case detail page's loaded state (M11B task §8):
 * sections A-H in order, each independently scoped so a missing/partial
 * array on one section never blocks the others from rendering (task §7:
 * "partial/missing data").
 */
export function DetailBody({ detail }: { detail: InvoiceDetailPayload }) {
  return (
    <div className={styles.body} data-testid="review-case-detail-body">
      <IdentitySection detail={detail} />
      <FieldsSection fields={[...detail.fields]} />
      <EvidenceSection
        sourceDocumentSha256={detail.source_document_sha256}
        originalDocumentUri={detail.original_document_uri}
        fields={[...detail.fields]}
        financialChecks={[...detail.financial_checks]}
      />
      <FinancialChecksSection checks={[...detail.financial_checks]} />
      <MatchingSection fields={[...detail.fields]} />
      <LineMatchesSection lineMatches={[...detail.line_matches]} />
      <TimelineSection timeline={[...detail.timeline]} />
      <ReviewDecisionsSection decisions={[...detail.review_decisions]} />
    </div>
  );
}
