import type { BackendHealthState } from "@/components/application-shell/BackendHealthIndicator";
import { PageHeader } from "@/components/application-shell/PageHeader";
import { EmptyState } from "@/components/feedback/EmptyState";
import { ErrorState } from "@/components/feedback/ErrorState";
import { OperationsConsole } from "@/components/operations/OperationsConsole";
import { getBackendHealth } from "@/lib/api/dashboard";
import { listJobs } from "@/lib/api/operations";
import { loadOperationsMode, operationsModesAgree } from "@/lib/config/operations-mode";
import { requirePageIdentity } from "@/lib/auth/identity";
import { issueCsrfToken } from "@/lib/server/csrf";
import { UPLOAD_CSRF_SCOPE } from "@/lib/server/upload-boundary";
import type { HealthCommandMode } from "@/types/api-payloads";
import styles from "./operations-page.module.css";

export const dynamic = "force-dynamic";

export default async function OperationsPage() {
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
  const backendOperations = healthResult.ok ? healthResult.data.operations_mode : null;

  const agree = operationsMode.valid && operationsModesAgree(operationsMode.mode, backendOperations);
  const jobsResult = agree && !configErrorMessage ? await listJobs() : null;

  let body;

  if (configErrorMessage) {
    body = <ErrorState kind="CONFIG_ERROR" />;
  } else if (!operationsMode.valid) {
    body = (
      <EmptyState
        title="Operations are misconfigured"
        description="The operations setting is invalid, so uploads are disabled. Ask an administrator to correct it."
      />
    );
  } else if (operationsMode.mode === "disabled") {
    body = (
      <EmptyState
        title="Operations are disabled"
        description="Invoice upload and processing are turned off on this deployment. Review pages remain available."
      />
    );
  } else if (!healthResult.ok) {
    body = <ErrorState kind={healthResult.kind} />;
  } else if (!agree) {
    body = (
      <EmptyState
        title="Operations modes do not match"
        description="The frontend and the backend are configured for different operations modes, so uploads are disabled. Ask an administrator to align them."
      />
    );
  } else if (jobsResult === null || !jobsResult.ok) {
    body = <ErrorState kind={jobsResult === null ? "UNKNOWN" : jobsResult.kind} />;
  } else {
    body = <OperationsConsole initialJobs={jobsResult.data.items} csrfToken={issueCsrfToken(UPLOAD_CSRF_SCOPE)} />;
  }

  return (
    <>
      <PageHeader
        title="Operations"
        description="Upload invoices, follow them through the pipeline and open the resulting review cases."
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
