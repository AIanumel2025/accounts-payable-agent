"use client";

import { forwardRef } from "react";
import type { CommandState } from "@/components/review-actions/useReviewCommand";
import { presentErrorCodes } from "@/lib/commands/messages";
import { humanizeCode, stageLabel } from "@/lib/formatting";
import styles from "./ReviewActions.module.css";

interface Props {
  state: CommandState;
  refreshRequired: boolean;
  onRefresh: () => void;
  /** M11D Core: a worker job consumes the handoff, so say "queued" rather than "not run". */
  operationsEnabled?: boolean;
}

/**
 * The single command-result region (M11C task §19/§21). Success renders in an
 * `aria-live` status region, errors in an alert region; both receive focus
 * after a submission (the parent focuses this element), and the wording never
 * lets validation be mistaken for execution.
 */
export const CommandResultBanner = forwardRef<HTMLDivElement, Props>(function CommandResultBanner(
  { state, refreshRequired, onRefresh, operationsEnabled = false },
  ref,
) {
  if (state.phase === "idle") {
    return <div ref={ref} tabIndex={-1} aria-live="polite" role="status" data-testid="command-result" />;
  }

  if (state.phase === "pending") {
    return (
      <div ref={ref} tabIndex={-1} role="status" aria-live="polite" className={`${styles.banner} ${styles.bannerInfo}`} data-testid="command-result">
        <p className={styles.bannerTitle}>Working…</p>
        <p className={styles.bannerText}>Sending the request. Please wait; do not resubmit.</p>
      </div>
    );
  }

  const { outcome } = state;

  if (!outcome.ok) {
    const presentations = presentErrorCodes(outcome.errors);
    return (
      <div ref={ref} tabIndex={-1} role="alert" className={`${styles.banner} ${styles.bannerError}`} data-testid="command-result" data-result="error">
        <p className={styles.bannerTitle}>The action was not completed</p>
        <ul className={styles.bannerList}>
          {presentations.map((presentation) => (
            <li key={presentation.title + presentation.description}>
              <strong>{presentation.title}.</strong> {presentation.description}
            </li>
          ))}
        </ul>
        {outcome.requestId ? (
          <p className={styles.tertiary}>Support reference: {outcome.requestId}</p>
        ) : null}
        {refreshRequired ? (
          <p>
            <button type="button" className={styles.button} onClick={onRefresh}>
              Refresh case data
            </button>
          </p>
        ) : null}
      </div>
    );
  }

  const { data, requestId } = outcome.success;

  if (data.kind === "VALIDATED") {
    return (
      <div ref={ref} tabIndex={-1} role="status" aria-live="polite" className={`${styles.banner} ${styles.bannerWarn}`} data-testid="command-result" data-result="validated">
        <p className={styles.bannerTitle}>Validated — no database changes were made.</p>
        <p className={styles.bannerText}>
          The command passed validation but was <strong>not executed</strong>. The case status has not changed, and no decision or audit event was recorded.
        </p>
        <p className={styles.tertiary}>Support reference: {requestId}</p>
      </div>
    );
  }

  const resumed = data.resume !== null;
  return (
    <div ref={ref} tabIndex={-1} role="status" aria-live="polite" className={`${styles.banner} ${styles.bannerSuccess}`} data-testid="command-result" data-result={data.kind === "IDEMPOTENT" ? "idempotent" : "executed"}>
      <p className={styles.bannerTitle}>
        {data.kind === "IDEMPOTENT"
          ? "Already recorded — this identical request was executed earlier; no duplicate was created."
          : "Executed — the database was updated and the action was recorded."}
      </p>
      <p className={styles.bannerText}>{data.message}</p>
      {resumed ? (
        <>
          <p className={styles.bannerText}>
            {operationsEnabled ? (
              <strong>
                The workflow-resume handoff was created and a worker job was queued. The pipeline has not resumed yet; the job status appears below.
              </strong>
            ) : (
              <strong>A controlled workflow-resume handoff was created. Downstream execution has not run yet.</strong>
            )}
          </p>
          <dl className={styles.resultDetails}>
            <dt>Restart stage</dt>
            <dd data-testid="resume-restart-stage">{data.resume?.restartStage ? stageLabel(data.resume.restartStage).label : "—"}</dd>
            <dt>Derived version</dt>
            <dd data-testid="resume-derived-version">{data.resume?.derivedVersion ?? "—"}</dd>
            <dt>Decision ID</dt>
            <dd>{data.decisionId ?? "—"}</dd>
            <dt>Resume plan</dt>
            <dd>{data.resume?.resumePlanId ?? "—"}</dd>
          </dl>
        </>
      ) : (
        <dl className={styles.resultDetails}>
          <dt>Resulting status</dt>
          <dd>{data.resultingCaseStatus ? humanizeCode(data.resultingCaseStatus) : "—"}</dd>
          {data.decisionId ? (
            <>
              <dt>Decision ID</dt>
              <dd>{data.decisionId}</dd>
            </>
          ) : null}
        </dl>
      )}
      <p className={styles.tertiary}>Support reference: {requestId}</p>
    </div>
  );
});
