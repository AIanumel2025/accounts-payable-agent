import { StatusBadge } from "@/components/ui/StatusBadge";
import { jobStatusPresentation } from "@/lib/operations/presentation";
import type { JobPayload } from "@/types/api-payloads";

export function JobStatusBadge({ job }: { job: Pick<JobPayload, "status" | "job_type"> }) {
  return (
    <span data-testid="job-status" data-status={job.status}>
      <StatusBadge presentation={jobStatusPresentation(job)} />
    </span>
  );
}
