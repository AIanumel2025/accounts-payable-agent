import type { BackendHealthState } from "@/components/application-shell/BackendHealthIndicator";
import { PageHeader } from "@/components/application-shell/PageHeader";
import { DashboardBody } from "@/components/dashboard/DashboardBody";
import { ErrorState } from "@/components/feedback/ErrorState";
import { getBackendHealth, getDashboard } from "@/lib/api/dashboard";
import { ServerConfigError, loadServerEnvConfig } from "@/lib/config/server-env";
import type { HealthCommandMode } from "@/types/api-payloads";
import styles from "./dashboard-page.module.css";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
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

  const [dashboardResult, healthResult] = await Promise.all([getDashboard(), getBackendHealth()]);

  const checkedAtIso = new Date().toISOString();
  const backendHealth: BackendHealthState = healthResult.ok
    ? { reachable: true, data: healthResult.data, checkedAtIso }
    : { reachable: false, data: null, checkedAtIso };

  const commandMode: HealthCommandMode | "UNKNOWN" = healthResult.ok ? healthResult.data.command_mode : "UNKNOWN";

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Operational overview of invoice processing and human-review load."
        actorRole={actorRole}
        backendHealth={backendHealth}
        commandMode={commandMode}
      />
      <div className={styles.content}>
        {configErrorMessage ? (
          <ErrorState kind="CONFIG_ERROR" />
        ) : !dashboardResult.ok ? (
          <ErrorState kind={dashboardResult.kind} />
        ) : (
          <DashboardBody dashboard={dashboardResult.data} />
        )}
      </div>
    </>
  );
}
