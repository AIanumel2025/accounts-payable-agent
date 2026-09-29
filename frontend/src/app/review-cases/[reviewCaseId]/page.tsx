import type { BackendHealthState } from "@/components/application-shell/BackendHealthIndicator";
import { Breadcrumb } from "@/components/application-shell/Breadcrumb";
import { PageHeader } from "@/components/application-shell/PageHeader";
import { DetailBody } from "@/components/review-detail/DetailBody";
import { ReviewActions } from "@/components/review-actions/ReviewActions";
import { ErrorState } from "@/components/feedback/ErrorState";
import { getBackendHealth } from "@/lib/api/dashboard";
import { getCommandCapabilities } from "@/lib/api/command-capabilities";
import { getReviewCaseDetail } from "@/lib/api/review-cases";
import { loadCommandMode } from "@/lib/config/command-mode";
import { deriveWorkspaceState, roleLabel } from "@/lib/commands/presentation";
import { issueCsrfToken } from "@/lib/server/csrf";
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

  const commandMode = loadCommandMode();
  const actionsEnabled = commandMode.valid && commandMode.mode !== "disabled";

  // Capabilities are only requested when actions could be shown at all -- a
  // disabled deployment behaves exactly like M11B (no extra backend call).
  const [detailResult, healthResult, capabilitiesResult] = await Promise.all([
    getReviewCaseDetail(reviewCaseId),
    getBackendHealth(),
    actionsEnabled ? getCommandCapabilities(reviewCaseId) : Promise.resolve(null),
  ]);

  const checkedAtIso = new Date().toISOString();
  const backendHealth: BackendHealthState = healthResult.ok
    ? { reachable: true, data: healthResult.data, checkedAtIso }
    : { reachable: false, data: null, checkedAtIso };
  const backendCommandMode: HealthCommandMode | "UNKNOWN" = healthResult.ok ? healthResult.data.command_mode : "UNKNOWN";
  const capabilities = capabilitiesResult && capabilitiesResult.ok ? capabilitiesResult.data : null;
  const workspaceState = deriveWorkspaceState({
    frontendMode: commandMode.mode,
    modeValid: commandMode.valid,
    capabilities,
  });
  const evidenceSuggestions: Record<string, string[]> = {};
  if (detailResult.ok) {
    for (const field of detailResult.data.fields) evidenceSuggestions[field.field_name] = [...field.evidence_reference_ids];
  }

  const title = detailResult.ok ? primaryLabel(detailResult.data) : "Review case";

  return (
    <>
      <Breadcrumb trail={[{ label: "Review queue", href: returnTo }]} current={title} />
      <PageHeader
        title={title}
        description={
          actionsEnabled
            ? "Invoice detail, evidence and review history, with controlled review actions."
            : "Read-only invoice detail and review history."
        }
        actorRole={actorRole}
        backendHealth={backendHealth}
        commandMode={backendCommandMode}
        frontendMode={commandMode.valid ? commandMode.mode : "disabled"}
      />
      <div className={styles.content}>
        {configErrorMessage ? (
          <ErrorState kind="CONFIG_ERROR" />
        ) : !detailResult.ok ? (
          <ErrorState kind={detailResult.kind} />
        ) : (
          <>
            <ReviewActions
              reviewCaseId={reviewCaseId}
              invoiceLabel={title}
              csrfToken={actionsEnabled ? issueCsrfToken(reviewCaseId) : ""}
              state={workspaceState}
              capabilities={capabilities}
              roleLabel={roleLabel(capabilities?.actor_role ?? actorRole)}
              frontendMode={commandMode.valid ? commandMode.mode : "disabled"}
              evidenceSuggestions={evidenceSuggestions}
            />
            <DetailBody detail={detailResult.data} />
          </>
        )}
      </div>
    </>
  );
}
