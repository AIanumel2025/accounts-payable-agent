"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import { JobStatusBadge } from "@/components/operations/JobStatusBadge";
import { stageLabel } from "@/lib/formatting";
import { fetchJobList } from "@/lib/operations/client";
import { isTerminalJob, jobErrorPresentation, jobOutcomeText } from "@/lib/operations/presentation";
import { usePolling } from "@/lib/operations/usePolling";
import type { JobPayload } from "@/types/api-payloads";
import styles from "./Operations.module.css";

/**
 * The state of the worker job that consumes this case's resume handoff
 * (M11D Core). Distinguishes "handoff created" (an M11C fact) from
 * queued / running / completed / returned to review / failed (worker
 * facts), and never says the pipeline resumed merely because the handoff
 * exists.
 */
export function ResumeJobPanel({
  reviewCaseId,
  initialJobs,
  pollIntervalMs,
  onSettled,
}: {
  reviewCaseId: string;
  initialJobs: JobPayload[];
  pollIntervalMs?: number;
  /** Called once when the resume job reaches a terminal state, so the page can re-read authoritative state. */
  onSettled?: () => void;
}) {
  const [jobs, setJobs] = useState<JobPayload[]>(initialJobs);

  const resumeJob = jobs.find((job) => job.job_type === "RESUME_WORKFLOW" && job.resumed_review_case_id === reviewCaseId);
  const originJob = jobs.find((job) => job.review_case_id === reviewCaseId);

  const refresh = useCallback(async () => {
    const result = await fetchJobList({ reviewCaseId });
    if (!result.ok) return;

    const before = jobs.find((job) => job.job_type === "RESUME_WORKFLOW" && job.resumed_review_case_id === reviewCaseId);
    const after = result.data.items.find((job) => job.job_type === "RESUME_WORKFLOW" && job.resumed_review_case_id === reviewCaseId);
    setJobs(result.data.items);

    if (before !== undefined && !isTerminalJob(before) && after !== undefined && isTerminalJob(after)) onSettled?.();
  }, [reviewCaseId, jobs, onSettled]);

  usePolling(refresh, resumeJob !== undefined && !isTerminalJob(resumeJob), pollIntervalMs);

  if (!resumeJob) {
    return originJob ? (
      <section className={styles.panel} aria-labelledby="origin-job-title" data-testid="origin-job-panel">
        <h2 id="origin-job-title" className={styles.panelTitle}>
          {originJob.job_type === "RESUME_WORKFLOW" ? "Opened by a workflow resume" : "Processing job"}
        </h2>
        <p className={styles.hint}>
          {originJob.job_type === "RESUME_WORKFLOW"
            ? "An earlier review was resolved and resumed, but downstream checks still required review, so this new case was opened. "
            : "This case came from an uploaded invoice. "}
          <Link href={`/operations/jobs/${originJob.job_id}`} data-testid="origin-job-link">
            View the job
          </Link>
        </p>
      </section>
    ) : null;
  }

  const failure = resumeJob.status === "FAILED" ? jobErrorPresentation(resumeJob.error_code) : null;
  const summary = resumeJob.summary;

  return (
    <section className={styles.panel} aria-labelledby="resume-job-title" data-testid="resume-job-panel" data-job-status={resumeJob.status}>
      <div className={styles.panelHeader}>
        <h2 id="resume-job-title" className={styles.panelTitle}>
          Workflow resume
        </h2>
        <button type="button" className={styles.button} onClick={() => void refresh()} data-testid="refresh-resume-job">
          Refresh
        </button>
      </div>
      <div>
        <JobStatusBadge job={resumeJob} />
      </div>
      <p className={styles.hint} data-testid="resume-job-outcome">
        {resumeJob.status === "QUEUED"
          ? "The resume handoff was created and is queued. The pipeline has not resumed yet."
          : jobOutcomeText(resumeJob)}
      </p>
      {summary.restart_stage ? (
        <p className={styles.muted} data-testid="resume-job-stages">
          Restarted at {stageLabel(summary.restart_stage).label}
          {summary.executed_stages.length > 0 ? ` · ran ${summary.executed_stages.map((stage) => stageLabel(stage).label).join(" → ")}` : ""}
        </p>
      ) : null}
      {resumeJob.review_case_id ? (
        <Link className={styles.linkButton} href={`/review-cases/${resumeJob.review_case_id}`} data-testid="new-review-link">
          Open the new review case
        </Link>
      ) : null}
      {failure ? (
        <p role="alert" className={`${styles.notice} ${styles.noticeError}`} data-testid="resume-job-failure">
          <strong>{failure.title}.</strong> {failure.description} {failure.nextAction}
        </p>
      ) : null}
      <Link href={`/operations/jobs/${resumeJob.job_id}`}>View the job and its timeline</Link>
    </section>
  );
}
