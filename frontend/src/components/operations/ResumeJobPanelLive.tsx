"use client";

import { useRouter } from "next/navigation";
import { useCallback, useTransition } from "react";
import { ResumeJobPanel } from "@/components/operations/ResumeJobPanel";
import type { JobPayload } from "@/types/api-payloads";

/**
 * Wires the resume-job panel to Next.js: when the worker finishes the job,
 * `router.refresh()` re-reads the case detail (workflow status, stage,
 * timeline) from PostgreSQL so the page never shows "Completed" next to a
 * stale "Processing" (nothing is optimistic).
 */
export function ResumeJobPanelLive(props: { reviewCaseId: string; initialJobs: JobPayload[] }) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const onSettled = useCallback(() => startTransition(() => router.refresh()), [router]);

  return <ResumeJobPanel {...props} onSettled={onSettled} />;
}
