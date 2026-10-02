"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, FormEvent } from "react";
import { JobList } from "@/components/operations/JobList";
import { fetchJobList, submitInvoice, submitInvoiceDirect } from "@/lib/operations/client";
import {
  formatFileSize,
  isTerminalJob,
  jobStatusPresentation,
  uploadErrorMessages,
  validateSelectedFile,
} from "@/lib/operations/presentation";
import { usePolling } from "@/lib/operations/usePolling";
import type { JobPayload } from "@/types/api-payloads";
import styles from "./Operations.module.css";

export interface OperationsConsoleProps {
  initialJobs: JobPayload[];
  csrfToken: string;
  /** How the file reaches storage; `s3_direct` is the AWS staged upload. Defaults to the multipart flow. */
  uploadMode?: "multipart" | "s3_direct";
  /** Test/demo hook only: shortens the polling interval. */
  pollIntervalMs?: number;
}

type Submission =
  | { phase: "idle" }
  | { phase: "submitting" }
  | { phase: "queued"; jobId: string; replay: boolean }
  | { phase: "error"; messages: string[] };

function newOperationId(): string {
  return crypto.randomUUID();
}

/**
 * Upload + recent jobs (M11D Core). Nothing is optimistic: a job is shown
 * as queued only after the server accepted it, and a terminal state only
 * after the worker wrote it. Polling is bounded, pauses on hidden pages and
 * stops once no job is active; a live region announces submission results
 * and terminal transitions only (never every refresh).
 */
export function OperationsConsole({ initialJobs, csrfToken, uploadMode = "multipart", pollIntervalMs }: OperationsConsoleProps) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const operationIdRef = useRef<string>(newOperationId());
  const knownStatusRef = useRef<Map<string, string>>(new Map(initialJobs.map((job) => [job.job_id, job.status])));

  const [jobs, setJobs] = useState<JobPayload[]>(initialJobs);
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [submission, setSubmission] = useState<Submission>({ phase: "idle" });
  const [dragging, setDragging] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [listError, setListError] = useState(false);
  // Hydration guard (M11E): until React has attached its handlers the file
  // input is disabled, so a choice can never be made against a control that
  // would silently drop it. A selection that still reaches the DOM before
  // hydration (e.g. set programmatically) is adopted in the effect below.
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    const result = await fetchJobList();
    if (!result.ok) {
      setListError(true);
      return;
    }
    setListError(false);
    setJobs(result.data.items);

    for (const job of result.data.items) {
      const previous = knownStatusRef.current.get(job.job_id);
      knownStatusRef.current.set(job.job_id, job.status);
      if (previous !== undefined && previous !== job.status && isTerminalJob(job)) {
        setAnnouncement(`${job.source_name}: ${jobStatusPresentation(job).label}.`);
      }
    }
  }, []);

  const hasActiveJob = jobs.some((job) => !isTerminalJob(job));
  usePolling(refresh, hasActiveJob, pollIntervalMs);

  const chooseFile = (candidate: File | null) => {
    setSubmission({ phase: "idle" });
    operationIdRef.current = newOperationId(); // a different file is a different operation
    setFile(candidate);
    setFileError(candidate === null ? null : validateSelectedFile(candidate));
    setAnnouncement(candidate === null ? "File removed." : `Selected ${candidate.name}.`);
  };

  useEffect(() => {
    const early = inputRef.current?.files?.[0] ?? null;
    if (early !== null) chooseFile(early);
    setReady(true);
    // Runs once, after hydration: `chooseFile` only uses state setters and refs.
  }, []);

  const onInputChange = (event: ChangeEvent<HTMLInputElement>) => chooseFile(event.target.files?.[0] ?? null);

  const onDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    setDragging(false);
    const dropped = event.dataTransfer.files?.[0] ?? null;
    if (dropped) chooseFile(dropped);
  };

  const clear = () => {
    if (inputRef.current) inputRef.current.value = "";
    chooseFile(null);
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (submission.phase === "submitting") return;

    if (file === null) {
      setFileError("Choose an invoice file first.");
      return;
    }

    const problem = validateSelectedFile(file);
    if (problem) {
      setFileError(problem);
      return;
    }

    setSubmission({ phase: "submitting" });
    const result =
      uploadMode === "s3_direct"
        ? await submitInvoiceDirect(file, csrfToken)
        : await submitInvoice(file, csrfToken, operationIdRef.current);

    if (!result.ok) {
      setSubmission({ phase: "error", messages: uploadErrorMessages(result.errors) });
      return;
    }

    const job = result.data.job;
    knownStatusRef.current.set(job.job_id, job.status);
    setJobs((current) => [job, ...current.filter((existing) => existing.job_id !== job.job_id)]);
    setSubmission({ phase: "queued", jobId: job.job_id, replay: result.data.idempotent_replay });
    setAnnouncement(
      result.data.idempotent_replay
        ? `This upload was already submitted as ${job.source_name}.`
        : `${job.source_name} was accepted and queued for processing.`,
    );
    if (inputRef.current) inputRef.current.value = "";
    setFile(null);
    operationIdRef.current = newOperationId();
  };

  const onRefreshClick = async () => {
    setRefreshing(true);
    await refresh();
    setRefreshing(false);
  };

  return (
    <div className={styles.stack}>
      <p className={styles.visuallyHidden} role="status" aria-live="polite" data-testid="operations-announcer">
        {announcement}
      </p>

      <section className={styles.panel} aria-labelledby="upload-title">
        <h2 id="upload-title" className={styles.panelTitle}>
          Upload an invoice
        </h2>
        <p className={styles.hint}>
          PDF, PNG or JPEG, up to 10 MB. The file is queued and processed by a separate worker through the
          existing pipeline; nothing is paid or posted anywhere.
        </p>

        <form onSubmit={onSubmit} noValidate aria-describedby={fileError ? "file-error" : undefined} data-testid="upload-form">
          <label
            htmlFor={inputId}
            className={styles.dropZone}
            data-dragging={dragging}
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <span className={styles.dropZoneTitle}>Choose a file, or drop it here</span>
            <input
              ref={inputRef}
              id={inputId}
              className={styles.fileInput}
              type="file"
              disabled={!ready}
              accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg"
              onChange={onInputChange}
              aria-invalid={fileError !== null}
              aria-describedby={fileError ? "file-error" : undefined}
              data-testid="file-input"
            />
          </label>

          {file ? (
            <dl className={styles.selected} data-testid="selected-file">
              <dt>File</dt>
              <dd>{file.name}</dd>
              <dt>Size</dt>
              <dd>{formatFileSize(file.size)}</dd>
            </dl>
          ) : null}

          {fileError ? (
            <p id="file-error" role="alert" className={`${styles.notice} ${styles.noticeError}`} data-testid="file-error">
              {fileError}
            </p>
          ) : null}

          <div className={styles.actions}>
            <button
              type="submit"
              className={`${styles.button} ${styles.primaryButton}`}
              disabled={file === null || fileError !== null || submission.phase === "submitting"}
              data-testid="process-button"
            >
              {submission.phase === "submitting" ? "Submitting…" : "Process invoice"}
            </button>
            {file ? (
              <button type="button" className={styles.button} onClick={clear} data-testid="remove-file">
                Remove file
              </button>
            ) : null}
          </div>
        </form>

        {submission.phase === "queued" ? (
          <p className={`${styles.notice} ${styles.noticeSuccess}`} data-testid="submission-result" data-result="queued">
            {submission.replay ? "This upload was already submitted. " : "Accepted and queued. "}
            The worker has not finished yet.{" "}
            <Link href={`/operations/jobs/${submission.jobId}`}>Follow this job</Link>
          </p>
        ) : null}

        {submission.phase === "error" ? (
          <div role="alert" className={`${styles.notice} ${styles.noticeError}`} data-testid="submission-result" data-result="error">
            <strong>The upload was not accepted.</strong>
            <ul className={styles.noticeList}>
              {submission.messages.map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      <section className={styles.panel} aria-labelledby="jobs-title">
        <div className={styles.panelHeader}>
          <h2 id="jobs-title" className={styles.panelTitle}>
            Recent jobs
          </h2>
          <button type="button" className={styles.button} onClick={onRefreshClick} disabled={refreshing} data-testid="refresh-jobs">
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        {hasActiveJob ? (
          <p className={styles.hint} data-testid="polling-note">
            Checking for updates automatically while jobs are running.
          </p>
        ) : null}
        {listError ? (
          <p role="alert" className={`${styles.notice} ${styles.noticeError}`} data-testid="list-error">
            The job list could not be refreshed. Showing the last known state.
          </p>
        ) : null}
        <JobList jobs={jobs} />
      </section>
    </div>
  );
}
