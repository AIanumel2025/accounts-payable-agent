"use client";

import Link from "next/link";
import { useCallback, useRef, useState } from "react";
import { JobStatusBadge } from "@/components/operations/JobStatusBadge";
import { JobTimeline } from "@/components/operations/JobTimeline";
import { fieldNameLabel, formatAbsoluteTimestamp, formatReviewReason, humanizeCode, stageLabel } from "@/lib/formatting";
import { fetchJobDetail } from "@/lib/operations/client";
import { isTerminalJob, jobErrorPresentation, jobOutcomeText, jobStatusPresentation, jobTypeLabel } from "@/lib/operations/presentation";
import { usePolling } from "@/lib/operations/usePolling";
import type { JobDetailPayload } from "@/types/api-payloads";
import styles from "./Operations.module.css";

function time(iso: string | null): string {
  if (!iso) return "—";
  const at = formatAbsoluteTimestamp(iso);
  return at.isValid ? at.display : "Unknown time";
}

export function JobDetailLive({ initial, pollIntervalMs }: { initial: JobDetailPayload; pollIntervalMs?: number }) {
  const [detail, setDetail] = useState<JobDetailPayload>(initial);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const statusRef = useRef(initial.job.status);

  const refresh = useCallback(async () => {
    const result = await fetchJobDetail(initial.job.job_id);
    if (!result.ok) {
      setError(true);
      return;
    }
    setError(false);
    setDetail(result.data);

    if (statusRef.current !== result.data.job.status) {
      statusRef.current = result.data.job.status;
      if (isTerminalJob(result.data.job)) setAnnouncement(`Job ${jobStatusPresentation(result.data.job).label}.`);
    }
  }, [initial.job.job_id]);

  const job = detail.job;
  usePolling(refresh, !isTerminalJob(job), pollIntervalMs);

  const summary = job.summary;
  const failure = job.status === "FAILED" ? jobErrorPresentation(job.error_code) : null;

  return (
    <div className={styles.stack} data-testid="job-detail" data-job-status={job.status}>
      <p className={styles.visuallyHidden} role="status" aria-live="polite" data-testid="job-announcer">
        {announcement}
      </p>

      <section className={styles.panel} aria-labelledby="job-overview">
        <div className={styles.panelHeader}>
          <h2 id="job-overview" className={styles.panelTitle}>
            {job.source_name}
          </h2>
          <button
            type="button"
            className={styles.button}
            disabled={refreshing}
            onClick={async () => {
              setRefreshing(true);
              await refresh();
              setRefreshing(false);
            }}
            data-testid="refresh-job"
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>

        <div>
          <JobStatusBadge job={job} />
        </div>
        <p className={styles.hint} data-testid="job-outcome">
          {jobOutcomeText(job)}
        </p>

        {error ? (
          <p role="alert" className={`${styles.notice} ${styles.noticeError}`} data-testid="job-refresh-error">
            The job could not be refreshed. Showing the last known state.
          </p>
        ) : null}

        <dl className={styles.definitionGrid}>
          <dt>Job type</dt>
          <dd>{jobTypeLabel(job.job_type)}</dd>
          <dt>Current stage</dt>
          <dd data-testid="job-stage">{job.current_stage ? stageLabel(job.current_stage).label : "—"}</dd>
          <dt>Attempts</dt>
          <dd>{job.attempt_count}</dd>
          <dt>Submitted</dt>
          <dd>{time(job.created_at)}</dd>
          <dt>Started</dt>
          <dd>{time(job.started_at)}</dd>
          <dt>Finished</dt>
          <dd>{time(job.completed_at)}</dd>
          <dt>Job ID</dt>
          <dd>{job.job_id}</dd>
        </dl>

        {job.review_case_id ? (
          <div data-testid="review-link">
            <Link className={styles.linkButton} href={`/review-cases/${job.review_case_id}`}>
              Open the review case
            </Link>
          </div>
        ) : null}
        {job.resumed_review_case_id ? (
          <p className={styles.hint}>
            Requested from{" "}
            <Link href={`/review-cases/${job.resumed_review_case_id}`} data-testid="resumed-from-link">
              the original review case
            </Link>
            .
          </p>
        ) : null}
      </section>

      {failure ? (
        <section className={`${styles.panel}`} aria-labelledby="job-failure" data-testid="job-failure">
          <h2 id="job-failure" className={styles.panelTitle}>
            {failure.title}
          </h2>
          <p className={styles.hint}>{failure.description}</p>
          <p className={styles.hint}>
            <strong>Next step:</strong> {failure.nextAction}
          </p>
          {job.error_code ? <p className={styles.muted}>Reference: {job.error_code}</p> : null}
        </section>
      ) : null}

      {job.status === "SUCCEEDED" || job.status === "REVIEW_REQUIRED" ? (
        <section className={styles.panel} aria-labelledby="job-result" data-testid="job-result">
          <h2 id="job-result" className={styles.panelTitle}>
            Result
          </h2>
          <dl className={styles.definitionGrid}>
            {summary.invoice_number ? (
              <>
                <dt>Invoice number</dt>
                <dd>{summary.invoice_number}</dd>
              </>
            ) : null}
            {summary.supplier_name ? (
              <>
                <dt>Supplier</dt>
                <dd>{summary.supplier_name}</dd>
              </>
            ) : null}
            {summary.total_amount ? (
              <>
                <dt>Total</dt>
                <dd>
                  {summary.currency ? `${summary.currency} ` : ""}
                  {summary.total_amount}
                </dd>
              </>
            ) : null}
            {summary.supplier_status ? (
              <>
                <dt>Supplier match</dt>
                <dd>{humanizeCode(summary.supplier_status)}</dd>
              </>
            ) : null}
            {summary.purchase_order_status ? (
              <>
                <dt>Purchase order</dt>
                <dd>{humanizeCode(summary.purchase_order_status)}</dd>
              </>
            ) : null}
            {summary.financial_validation_status ? (
              <>
                <dt>Financial validation</dt>
                <dd>{humanizeCode(summary.financial_validation_status)}</dd>
              </>
            ) : null}
            {summary.review_reasons.length > 0 ? (
              <>
                <dt>Review reasons</dt>
                <dd>{summary.review_reasons.slice(0, 6).map(formatReviewReason).join(", ")}{summary.review_reasons.length > 6 ? `, +${summary.review_reasons.length - 6} more` : ""}</dd>
              </>
            ) : null}
          </dl>
        </section>
      ) : null}

      {job.job_type === "RESUME_WORKFLOW" && summary.restart_stage ? (
        <section className={styles.panel} aria-labelledby="job-resume" data-testid="job-resume">
          <h2 id="job-resume" className={styles.panelTitle}>
            Resume provenance
          </h2>
          <dl className={styles.definitionGrid}>
            <dt>Restart stage</dt>
            <dd data-testid="resume-stage">{stageLabel(summary.restart_stage).label}</dd>
            {summary.executed_stages.length > 0 ? (
              <>
                <dt>Stages executed</dt>
                <dd data-testid="resume-executed">{summary.executed_stages.map((stage) => stageLabel(stage).label).join(" → ")}</dd>
              </>
            ) : null}
            {summary.corrected_fields.length > 0 ? (
              <>
                <dt>Corrected fields</dt>
                <dd>{summary.corrected_fields.map(fieldNameLabel).join(", ")}</dd>
              </>
            ) : null}
            {summary.derived_version ? (
              <>
                <dt>Derived version</dt>
                <dd>{summary.derived_version}</dd>
              </>
            ) : null}
            {summary.decision_id ? (
              <>
                <dt>Decision</dt>
                <dd>{summary.decision_id}</dd>
              </>
            ) : null}
          </dl>
          <p className={styles.muted}>The original extraction is never modified; the reviewed result is stored as a separate derived version.</p>
        </section>
      ) : null}

      <section className={styles.panel} aria-labelledby="job-timeline-title">
        <h2 id="job-timeline-title" className={styles.panelTitle}>
          Timeline
        </h2>
        <JobTimeline events={detail.events} />
      </section>
    </div>
  );
}
