import type { BackendHealthState } from "@/components/application-shell/BackendHealthIndicator";
import { Breadcrumb } from "@/components/application-shell/Breadcrumb";
import { PageHeader } from "@/components/application-shell/PageHeader";
import { DetailBody } from "@/components/review-detail/DetailBody";
import { ErrorState } from "@/components/feedback/ErrorState";
import { getBackendHealth } from "@/lib/api/dashboard";
import { getReviewCaseDetail } from "@/lib/api/review-cases";
import { parseSafeReturnTo } from "@/lib/api/review-queue-query";
import { ServerConfigError, loadServerEnvConfig } from "@/lib/config/server-env";
import type { HealthCommandMode, InvoiceDetailPayload } from "@/types/api-payloads";
import styles from "./review-case-detail-page.module.css";

export const dynamic = "force-dynamic";

/** Never a raw internal ID as the primary label when a meaningful one exists (task §8/§12), matching the queue's own `primaryLabel`. */
function primaryLabel(detail: InvoiceDetailPayload): string {
  const invoiceNumberField = detail.fields.find((field) => field.field_name === "INVOICE_NUMBER");
  return invoiceNumberField?.normalized_value ?? detail.source_name;
}

export default async function ReviewCaseDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ reviewCaseId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { reviewCaseId } = await params;
  const resolvedSearchParams = await searchParams;
  const returnTo = parseSafeReturnTo(resolvedSearchParams.from);

  let actorRole = "UNKNOWN";
  let configErrorMessage: string | null = null;
  try {
    actorRole = loadServerEnvConfig().devActorRole;
  } catch (error) {
    if (error instanceof ServerConfigError) {
      configErrorMessage = error.message;
    } else {
      throw error;
    }
  }

  const [detailResult, healthResult] = await Promise.all([getReviewCaseDetail(reviewCaseId), getBackendHealth()]);

  const checkedAtIso = new Date().toISOString();
  const backendHealth: BackendHealthState = healthResult.ok
    ? { reachable: true, data: healthResult.data, checkedAtIso }
    : { reachable: false, data: null, checkedAtIso };
  const commandMode: HealthCommandMode | "UNKNOWN" = healthResult.ok ? healthResult.data.command_mode : "UNKNOWN";

  const title = detailResult.ok ? primaryLabel(detailResult.data) : "Review case";

  return (
    <>
      <Breadcrumb trail={[{ label: "Review queue", href: returnTo }]} current={title} />
      <PageHeader
        title={title}
        description="Read-only invoice detail and review history. Claim, decision, and correction controls arrive in a later milestone."
        actorRole={actorRole}
        backendHealth={backendHealth}
        commandMode={commandMode}
      />
      <div className={styles.content}>
        {configErrorMessage ? (
          <ErrorState kind="CONFIG_ERROR" />
        ) : !detailResult.ok ? (
          <ErrorState kind={detailResult.kind} />
        ) : (
          <DetailBody detail={detailResult.data} />
        )}
      </div>
    </>
  );
}
