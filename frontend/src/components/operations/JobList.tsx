import Link from "next/link";
import { EmptyState } from "@/components/feedback/EmptyState";
import { JobStatusBadge } from "@/components/operations/JobStatusBadge";
import { formatAbsoluteTimestamp, stageLabel } from "@/lib/formatting";
import { jobOutcomeText, jobTypeLabel } from "@/lib/operations/presentation";
import type { JobPayload } from "@/types/api-payloads";
import styles from "./Operations.module.css";

function stageText(job: JobPayload): string {
  return job.current_stage ? stageLabel(job.current_stage).label : "—";
}

function submitted(job: JobPayload): string {
  const at = formatAbsoluteTimestamp(job.created_at);
  return at.isValid ? at.display : "Unknown time";
}

function ReviewLink({ job }: { job: JobPayload }) {
  if (!job.review_case_id) return <span className={styles.muted}>—</span>;
  return <Link href={`/review-cases/${job.review_case_id}`}>Open review case</Link>;
}

/** Recent jobs: a table on wide screens, stacked cards on narrow ones (same data, both in the DOM order a screen reader expects). */
export function JobList({ jobs }: { jobs: readonly JobPayload[] }) {
  if (jobs.length === 0) {
    return <EmptyState title="No jobs yet" description="Upload an invoice above to see it move through the pipeline." />;
  }

  return (
    <>
      <div className={styles.tableWrapper}>
        <table className={styles.table} data-testid="job-table">
          <caption className={styles.visuallyHidden}>Recent jobs</caption>
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">Type</th>
              <th scope="col">Status</th>
              <th scope="col">Stage</th>
              <th scope="col">Submitted</th>
              <th scope="col">Outcome</th>
              <th scope="col">Links</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.job_id} data-testid="job-row" data-job-id={job.job_id}>
                <td>
                  <Link href={`/operations/jobs/${job.job_id}`}>{job.source_name}</Link>
                </td>
                <td>{jobTypeLabel(job.job_type)}</td>
                <td>
                  <JobStatusBadge job={job} />
                </td>
                <td>{stageText(job)}</td>
                <td>{submitted(job)}</td>
                <td>{jobOutcomeText(job)}</td>
                <td>
                  <ReviewLink job={job} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className={styles.cardList} data-testid="job-cards" aria-label="Recent jobs">
        {jobs.map((job) => (
          <li key={job.job_id} className={styles.card} data-job-id={job.job_id}>
            <Link className={styles.cardTitle} href={`/operations/jobs/${job.job_id}`}>
              {job.source_name}
            </Link>
            <span className={styles.muted}>
              {jobTypeLabel(job.job_type)} · {submitted(job)}
            </span>
            <JobStatusBadge job={job} />
            <span>Stage: {stageText(job)}</span>
            <span>{jobOutcomeText(job)}</span>
            <ReviewLink job={job} />
          </li>
        ))}
      </ul>
    </>
  );
}
