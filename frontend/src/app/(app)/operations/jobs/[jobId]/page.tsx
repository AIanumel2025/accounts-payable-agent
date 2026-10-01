import type { BackendHealthState } from "@/components/application-shell/BackendHealthIndicator";
import { Breadcrumb } from "@/components/application-shell/Breadcrumb";
import { PageHeader } from "@/components/application-shell/PageHeader";
import { EmptyState } from "@/components/feedback/EmptyState";
import { ErrorState } from "@/components/feedback/ErrorState";
import { JobDetailLive } from "@/components/operations/JobDetailLive";
import { getBackendHealth } from "@/lib/api/dashboard";
import { getJobDetail } from "@/lib/api/operations";
import { loadOperationsMode, operationsModesAgree } from "@/lib/config/operations-mode";
import { requirePageIdentity } from "@/lib/auth/identity";
import type { HealthCommandMode } from "@/types/api-payloads";
import styles from "./job-detail-page.module.css";

export const dynamic = "force-dynamic";

export default async function JobDetailPage({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await params;

  // M11E: development role in local/test mode; in hosted mode the role resolved by FastAPI's database mapping.
  const identity = await requirePageIdentity();
  const actorRole = identity.role;
  const configErrorMessage = identity.configErrorMessage;

  const operationsMode = loadOperationsMode();
  const healthResult = await getBackendHealth();
  const checkedAtIso = new Date().toISOString();
  const backendHealth: BackendHealthState = healthResult.ok
    ? { reachable: true, data: healthResult.data, checkedAtIso }
    : { reachable: false, data: null, checkedAtIso };
  const commandMode: HealthCommandMode | "UNKNOWN" = healthResult.ok ? healthResult.data.command_mode : "UNKNOWN";
  const agree =
    operationsMode.valid && operationsModesAgree(operationsMode.mode, healthResult.ok ? healthResult.data.operations_mode : null);

  const detailResult = agree && !configErrorMessage ? await getJobDetail(jobId) : null;
  const title = detailResult && detailResult.ok ? detailResult.data.job.source_name : "Job";

  let body;
  if (configErrorMessage) {
    body = <ErrorState kind="CONFIG_ERROR" />;
  } else if (!agree) {
    body = (
      <EmptyState
        title="Operations are not available"
        description="Operations are disabled, misconfigured or do not match the backend, so job details cannot be shown."
      />
    );
  } else if (detailResult === null || !detailResult.ok) {
    body = <ErrorState kind={detailResult === null ? "UNKNOWN" : detailResult.kind} />;
  } else {
    body = <JobDetailLive initial={detailResult.data} />;
  }

  return (
    <>
      <Breadcrumb trail={[{ label: "Operations", href: "/operations" }]} current={title} />
      <PageHeader
        title={title}
        description="Job status, stage timeline and outcome."
        actorRole={actorRole}
        tenantName={identity.tenantName}
        hosted={identity.hosted}
        backendHealth={backendHealth}
        commandMode={commandMode}
      />
      <div className={styles.content}>{body}</div>
    </>
  );
}
