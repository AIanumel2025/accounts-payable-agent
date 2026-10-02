import type { BackendHealthState } from "@/components/application-shell/BackendHealthIndicator";
import { PageHeader } from "@/components/application-shell/PageHeader";
import { EmptyState } from "@/components/feedback/EmptyState";
import { ErrorState } from "@/components/feedback/ErrorState";
import { QueueFilters } from "@/components/review-queue/QueueFilters";
import { QueuePagination } from "@/components/review-queue/QueuePagination";
import { QueueResultsTable } from "@/components/review-queue/QueueResultsTable";
import { getBackendHealth } from "@/lib/api/dashboard";
import { listReviewCases } from "@/lib/api/review-cases";
import { filtersToUrlSearchParams, hasActiveFilters, parseQueueFiltersFromSearchParams } from "@/lib/api/review-queue-query";
import { requirePageIdentity } from "@/lib/auth/identity";
import type { HealthCommandMode } from "@/types/api-payloads";
import styles from "./review-queue-page.module.css";

export const dynamic = "force-dynamic";

function toUrlSearchParams(resolved: Record<string, string | string[] | undefined>): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(resolved)) {
    if (typeof value === "string") {
      params.set(key, value);
    } else if (Array.isArray(value) && value.length > 0 && value[0] !== undefined) {
      // Task §5: repeated query parameters are accepted only where the API
      // contract permits it; none of these filters are repeatable, so only
      // the first occurrence is honoured -- never silently combined.
      params.set(key, value[0]);
    }
  }
  return params;
}

export default async function ReviewQueuePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const resolvedSearchParams = await searchParams;
  const { filters } = parseQueueFiltersFromSearchParams(toUrlSearchParams(resolvedSearchParams));
  const returnTo = `/review-queue?${filtersToUrlSearchParams(filters).toString()}`;

  // M11E: development role in local/test mode; in hosted mode the role resolved by FastAPI's database mapping.
  const identity = await requirePageIdentity();
  const actorRole = identity.role;
  const configErrorMessage = identity.configErrorMessage;

  const [queueResult, healthResult] = await Promise.all([listReviewCases(filters), getBackendHealth()]);

  const checkedAtIso = new Date().toISOString();
  const backendHealth: BackendHealthState = healthResult.ok
    ? { reachable: true, data: healthResult.data, checkedAtIso }
    : { reachable: false, data: null, checkedAtIso };
  const commandMode: HealthCommandMode | "UNKNOWN" = healthResult.ok ? healthResult.data.command_mode : "UNKNOWN";

  return (
    <>
      <PageHeader
        title="Review queue"
        description="Invoices currently awaiting human review. Read-only: claim, release, and decision controls arrive in a later milestone."
        actorRole={actorRole}
        tenantName={identity.tenantName}
        hosted={identity.hosted}
        backendHealth={backendHealth}
        commandMode={commandMode}
      />
      <div className={styles.content}>
        {configErrorMessage ? (
          <ErrorState kind="CONFIG_ERROR" />
        ) : !queueResult.ok ? (
          <ErrorState kind={queueResult.kind} />
        ) : (
          <>
            <QueueFilters filters={filters} resultCount={queueResult.data.pagination.total_count} />
            {queueResult.data.items.length === 0 ? (
              <EmptyState
                title={hasActiveFilters(filters) ? "No matching review cases" : "Review queue is empty"}
                description={
                  hasActiveFilters(filters)
                    ? "No review cases match the current filters. Try clearing a filter."
                    : "There are no invoices awaiting human review right now."
                }
              />
            ) : (
              <>
                <QueueResultsTable items={queueResult.data.items} returnTo={returnTo} />
                <QueuePagination pagination={queueResult.data.pagination} filters={filters} />
              </>
            )}
          </>
        )}
      </div>
    </>
  );
}
