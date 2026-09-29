"use client";

import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import {
  clearedFilters,
  filtersToUrlSearchParams,
  hasActiveFilters,
  SUPPORTED_QUEUE_PRIORITY_FILTERS,
  SUPPORTED_QUEUE_STATUS_FILTERS,
  type ReviewQueueFilters,
} from "@/lib/api/review-queue-query";
import { priorityLabel, reviewCaseStatusLabel } from "@/lib/formatting";
import styles from "./QueueFilters.module.css";

/**
 * Server-driven review-queue filters (M11B task §7). Every change
 * navigates to a new URL (never filters the currently loaded page in
 * memory) -- the Server Component re-fetches from FastAPI with the new
 * query parameters. Filter state lives entirely in the URL, so it
 * survives a reload and a browser-back from a detail page.
 */
export function QueueFilters({ filters, resultCount }: { filters: ReviewQueueFilters; resultCount?: number }) {
  const router = useRouter();
  const [assignedToDraft, setAssignedToDraft] = useState(filters.assignedTo ?? "");
  const [batchIdDraft, setBatchIdDraft] = useState(filters.batchId ?? "");
  const statusId = useId();
  const priorityId = useId();
  const assignedId = useId();
  const batchId = useId();

  function navigate(next: ReviewQueueFilters) {
    const params = filtersToUrlSearchParams(next);
    const query = params.toString();
    router.push(query ? `/review-queue?${query}` : "/review-queue");
  }

  function handleStatusChange(event: React.ChangeEvent<HTMLSelectElement>) {
    const value = event.target.value;
    navigate({
      ...filters,
      page: 1,
      status: value === "" ? undefined : (value as ReviewQueueFilters["status"]),
    });
  }

  function handlePriorityChange(event: React.ChangeEvent<HTMLSelectElement>) {
    const value = event.target.value;
    navigate({
      ...filters,
      page: 1,
      priority: value === "" ? undefined : (value as ReviewQueueFilters["priority"]),
    });
  }

  function handleAssignedSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    navigate({ ...filters, page: 1, assignedTo: assignedToDraft.trim() === "" ? undefined : assignedToDraft.trim() });
  }

  function handleBatchSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    navigate({ ...filters, page: 1, batchId: batchIdDraft.trim() === "" ? undefined : batchIdDraft.trim() });
  }

  function handleClearAll() {
    setAssignedToDraft("");
    setBatchIdDraft("");
    navigate(clearedFilters(filters));
  }

  return (
    <section className={styles.filters} aria-label="Review queue filters">
      <div className={styles.controls}>
        <div className={styles.control}>
          <label htmlFor={statusId}>Status</label>
          <select id={statusId} value={filters.status ?? ""} onChange={handleStatusChange}>
            <option value="">All statuses</option>
            {SUPPORTED_QUEUE_STATUS_FILTERS.map((status) => (
              <option key={status} value={status}>
                {reviewCaseStatusLabel(status).label}
              </option>
            ))}
          </select>
        </div>

        <div className={styles.control}>
          <label htmlFor={priorityId}>Priority</label>
          <select id={priorityId} value={filters.priority ?? ""} onChange={handlePriorityChange}>
            <option value="">All priorities</option>
            {SUPPORTED_QUEUE_PRIORITY_FILTERS.map((priority) => (
              <option key={priority} value={priority}>
                {priorityLabel(priority).label}
              </option>
            ))}
          </select>
        </div>

        <form className={styles.control} onSubmit={handleAssignedSubmit}>
          <label htmlFor={assignedId}>Assigned to</label>
          <div className={styles.inputRow}>
            <input
              id={assignedId}
              type="text"
              value={assignedToDraft}
              onChange={(event) => setAssignedToDraft(event.target.value)}
              placeholder="Reviewer id"
            />
            <button type="submit">Apply</button>
          </div>
        </form>

        <form className={styles.control} onSubmit={handleBatchSubmit}>
          <label htmlFor={batchId}>Batch ID</label>
          <div className={styles.inputRow}>
            <input
              id={batchId}
              type="text"
              value={batchIdDraft}
              onChange={(event) => setBatchIdDraft(event.target.value)}
              placeholder="UUID"
            />
            <button type="submit">Apply</button>
          </div>
        </form>

        <button
          type="button"
          className={styles.clearButton}
          onClick={handleClearAll}
          disabled={!hasActiveFilters(filters)}
        >
          Clear all filters
        </button>
      </div>

      {resultCount !== undefined ? (
        <p className={styles.resultCount} role="status">
          {resultCount} matching {resultCount === 1 ? "case" : "cases"}
        </p>
      ) : null}
    </section>
  );
}
