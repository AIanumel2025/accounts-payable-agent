import { fieldNameLabel, formatConfidence } from "@/lib/formatting";
import { MISSING_VALUE_DISPLAY } from "@/lib/formatting/money";
import type { InterfaceFieldValuePayload } from "@/types/api-payloads";
import styles from "./DetailSections.module.css";

const SUPPLIER_PO_FIELD_NAMES = ["SUPPLIER_NAME", "SUPPLIER_ADDRESS", "PURCHASE_ORDER_NUMBER"];

/**
 * Section E (M11B task §8E): supplier/PO/receipt matching.
 *
 * **Backend contract finding (task §4):** `InvoiceDetailResponse`
 * (`src/ap_agent/api/schemas.py`) has no `supplier_status`,
 * `purchase_order_status`, or `goods_receipt_status` field -- those
 * resolution-status codes exist only on `ReviewQueueItem`, the *list*
 * endpoint's response (`ReviewQueueRecord`, `src/ap_agent/models/
 * interface.py` lines 300-322). `InvoiceDetailRecord` (same file, lines
 * 389-412) carries no goods-receipt data at all, and `InvoiceFieldName`
 * (`src/ap_agent/models/normalization.py`) has no goods-receipt field
 * either. This section therefore shows the actual extracted
 * supplier/purchase-order *field values* the detail endpoint does return,
 * plus an explicit note about the missing status codes, rather than
 * inventing or silently re-deriving a resolution status this endpoint
 * does not provide (CLAUDE.md: "never invent parameter names or enum
 * values"; "Known, deliberate deviations ... must be documented, not
 * silently fixed").
 */
export function MatchingSection({ fields }: { fields: InterfaceFieldValuePayload[] }) {
  const relevantFields = fields.filter((fieldValue) => SUPPLIER_PO_FIELD_NAMES.includes(fieldValue.field_name));

  return (
    <section aria-labelledby="section-matching" className={styles.section}>
      <h2 id="section-matching" className={styles.sectionTitle}>
        Supplier and purchase-order matching
      </h2>
      <p className={styles.sectionNote}>
        The review-case detail API does not return dedicated supplier/purchase-order/goods-receipt resolution
        statuses (those are only available on the review queue list). This section shows the extracted
        supplier/purchase-order field values instead; open this case from the queue to see its resolution status
        badges.
      </p>
      {relevantFields.length === 0 ? (
        <p className={styles.muted}>No supplier or purchase-order fields were extracted for this document.</p>
      ) : (
        <dl className={styles.identityGrid}>
          {relevantFields.map((fieldValue) => {
            const confidence = formatConfidence(fieldValue.confidence);
            const hasValue = fieldValue.normalized_value !== null;
            return (
              <div key={fieldValue.field_name}>
                <dt>{fieldNameLabel(fieldValue.field_name)}</dt>
                <dd className={hasValue ? undefined : styles.muted}>
                  {hasValue ? fieldValue.normalized_value : MISSING_VALUE_DISPLAY}
                  {hasValue ? (
                    <>
                      {" · "}
                      <span className={confidence.isLow ? styles.low : undefined}>{confidence.display} confidence</span>
                    </>
                  ) : null}
                </dd>
              </div>
            );
          })}
        </dl>
      )}
    </section>
  );
}
