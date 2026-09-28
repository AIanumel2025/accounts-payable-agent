"use client";

import { useRouter } from "next/navigation";
import { filtersToUrlSearchParams, type ReviewQueueFilters } from "@/lib/api/review-queue-query";
import type { PaginationMetaPayload } from "@/types/api-payloads";
import styles from "./QueuePagination.module.css";

/** Stable, server-driven pagination (M11B task §7): page state lives in the URL, the backend owns ordering, so traversal never produces duplicate rows. */
export function QueuePagination({
  pagination,
  filters,
}: {
  pagination: PaginationMetaPayload;
  filters: ReviewQueueFilters;
}) {
  const router = useRouter();

  function goToPage(page: number) {
    const params = filtersToUrlSearchParams({ ...filters, page });
    const query = params.toString();
    router.push(query ? `/review-queue?${query}` : "/review-queue");
  }

  const canGoPrevious = pagination.page > 1;
  const canGoNext = pagination.page < pagination.total_pages;

  return (
    <nav className={styles.pagination} aria-label="Review queue pages">
      <p className={styles.summary}>
        Page {pagination.page} of {pagination.total_pages} &middot; {pagination.total_count}{" "}
        {pagination.total_count === 1 ? "result" : "results"} total
      </p>
      <div className={styles.buttons}>
        <button type="button" disabled={!canGoPrevious} onClick={() => goToPage(pagination.page - 1)}>
          <span aria-hidden="true">&larr;</span> Previous
        </button>
        <button type="button" disabled={!canGoNext} onClick={() => goToPage(pagination.page + 1)}>
          Next <span aria-hidden="true">&rarr;</span>
        </button>
      </div>
    </nav>
  );
}
