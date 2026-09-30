"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { CommandResultBanner } from "@/components/review-actions/CommandResultBanner";
import { ConfirmDialog } from "@/components/review-actions/ConfirmDialog";
import { CorrectionEditor } from "@/components/review-actions/CorrectionEditor";
import { ReasonCodePicker } from "@/components/review-actions/ReasonCodePicker";
import { useReviewCommand } from "@/components/review-actions/useReviewCommand";
import styles from "@/components/review-actions/ReviewActions.module.css";
import type { SubmitCommand } from "@/lib/commands/client";
import { ACTION_LABELS, type CorrectionDraft, type SupportedAction } from "@/lib/commands/contract";
import {
  emptyRow,
  hasUnsavedCorrections,
  rowsToCorrections,
  validateCorrections,
  type CorrectionRow,
  type CorrectionValidation,
} from "@/lib/commands/corrections";
import { errorsNeedRefresh } from "@/lib/commands/messages";
import type { CommandContent } from "@/lib/commands/identity";
import { ASSIGNMENT_LABELS, RESUME_INELIGIBLE_COPY, type WorkspaceState } from "@/lib/commands/presentation";
import {
  APPROVE_REASON_CODES,
  CORRECT_REASON_CODES,
  REJECT_REASON_CODES,
  RESUME_REASON_CODES,
} from "@/lib/commands/reason-codes";
import { reviewCaseStatusLabel } from "@/lib/formatting";
import type { CommandCapabilitiesPayload } from "@/types/api-payloads";

export interface ReviewActionWorkspaceProps {
  reviewCaseId: string;
  invoiceLabel: string;
  csrfToken: string;
  state: WorkspaceState;
  capabilities: CommandCapabilitiesPayload | null;
  roleLabel: string;
  frontendMode: "disabled" | "validation_only" | "commit";
  evidenceSuggestions: Record<string, string[]>;
  /** M11D Core: a worker job consumes the resume handoff (operations mode enabled and agreeing). */
  operationsEnabled?: boolean;
  /** Re-reads authoritative server state (Next.js `router.refresh()` in production). */
  onRefresh: () => void;
  /** Injectable for tests; defaults to the real same-origin POST. */
  submit?: SubmitCommand;
}

type OpenDialog = null | "approve" | "reject" | "correct" | "release";

export function ReviewActionWorkspace(props: ReviewActionWorkspaceProps) {
  const { reviewCaseId, invoiceLabel, csrfToken, state, capabilities, roleLabel, frontendMode, evidenceSuggestions, onRefresh, submit } = props;
  const headingId = useId();
  const resultRef = useRef<HTMLDivElement>(null);
  const rowCounter = useRef(0);
  const newRowId = useCallback(() => `row-${(rowCounter.current += 1)}`, []);

  const [dialog, setDialog] = useState<OpenDialog>(null);
  const [showEditor, setShowEditor] = useState(false);
  const [rows, setRows] = useState<CorrectionRow[]>([]);
  const [validation, setValidation] = useState<CorrectionValidation | null>(null);
  const [approveReasons, setApproveReasons] = useState<string[]>([]);
  const [approveNotes, setApproveNotes] = useState("");
  const [rejectReasons, setRejectReasons] = useState<string[]>([]);
  const [rejectNotes, setRejectNotes] = useState("");
  const [correctReasons, setCorrectReasons] = useState<string[]>([]);
  const [correctNotes, setCorrectNotes] = useState("");
  const [resumeReasons, setResumeReasons] = useState<string[]>(RESUME_REASON_CODES.map((option) => option.code));
  const [formError, setFormError] = useState<string | null>(null);
  const [staleAt, setStaleAt] = useState<string | null>(null);
  const editorRef = useRef<HTMLFieldSetElement>(null);
  const summaryRef = useRef<HTMLDivElement>(null);
  const [invalidAttempts, setInvalidAttempts] = useState(0);

  const revisionKey = capabilities ? `${capabilities.review_revision}:${capabilities.workflow_revision}` : "";

  const resetForms = useCallback(() => {
    setDialog(null);
    setShowEditor(false);
    setRows([]);
    setValidation(null);
    setApproveReasons([]);
    setApproveNotes("");
    setRejectReasons([]);
    setRejectNotes("");
    setCorrectReasons([]);
    setCorrectNotes("");
    setFormError(null);
  }, []);

  const { state: commandState, run } = useReviewCommand({
    reviewCaseId,
    csrfToken,
    submit,
    onExecuted: () => {
      resetForms();
      onRefresh();
    },
  });

  // Move focus to the result region after every completed submission (task §21).
  const doneSeq = commandState.phase === "done" ? commandState.seq : 0;
  useEffect(() => {
    if (doneSeq > 0) resultRef.current?.focus();
  }, [doneSeq]);

  // Opening the correction editor moves focus to its first field.
  useEffect(() => {
    if (showEditor) editorRef.current?.querySelector("select")?.focus();
  }, [showEditor]);

  // After a failed client-side validation, move focus to the error summary (task §21).
  useEffect(() => {
    if (invalidAttempts > 0) summaryRef.current?.focus();
  }, [invalidAttempts]);

  // A conflict that needs fresh data locks submission until the server props
  // (and so the observed revisions) change -- derived, not an effect (task §17).
  const refreshRequired = staleAt !== null && staleAt === revisionKey;

  const pending = commandState.phase === "pending";
  const locked = pending || refreshRequired;

  const send = useCallback(
    async (content: Omit<CommandContent, "observed_review_revision" | "observed_workflow_revision">) => {
      if (!capabilities) return null;
      const outcome = await run({
        ...content,
        observed_review_revision: capabilities.review_revision,
        observed_workflow_revision: capabilities.workflow_revision,
      });
      if (outcome && !outcome.ok && errorsNeedRefresh(outcome.errors)) {
        setStaleAt(`${capabilities.review_revision}:${capabilities.workflow_revision}`);
      }
      return outcome;
    },
    [capabilities, run],
  );

  const status = capabilities ? reviewCaseStatusLabel(capabilities.case_status) : null;

  return (
    <section aria-labelledby={headingId} className={styles.workspace} data-testid="review-action-workspace">
      <h2 id={headingId} className={styles.title}>Review actions</h2>

      {capabilities ? (
        <dl className={styles.summary} data-testid="action-summary">
          <div><dt>Case status</dt><dd>{status?.label}</dd></div>
          <div><dt>Assignment</dt><dd>{ASSIGNMENT_LABELS[capabilities.assignment]}</dd></div>
          <div><dt>Your role</dt><dd>{roleLabel}</dd></div>
          <div><dt>Review revision</dt><dd>{capabilities.review_revision}</dd></div>
          <div><dt>Workflow revision</dt><dd>{capabilities.workflow_revision}</dd></div>
          <div>
            <dt>Command mode</dt>
            <dd>{frontendMode === "commit" ? "Commit (writes enabled)" : frontendMode === "validation_only" ? "Validation only" : "Disabled"}</dd>
          </div>
        </dl>
      ) : null}

      <ModeBanner state={state} frontendMode={frontendMode} />

      <CommandResultBanner ref={resultRef} state={commandState} refreshRequired={refreshRequired} onRefresh={onRefresh} operationsEnabled={props.operationsEnabled ?? false} />

      {refreshRequired ? (
        <p className={styles.note} role="note" data-testid="refresh-required">
          Actions are paused. Refresh the case data and review what changed before trying again — nothing is retried automatically, and your form entries are kept.
        </p>
      ) : null}

      {state.kind === "ready" && capabilities ? (
        <ReadyControls
          state={state}
          capabilities={capabilities}
          locked={locked}
          pending={pending}
          showEditor={showEditor}
          hasUnsaved={hasUnsavedCorrections(rows)}
          onClaim={() => void send({ action: "CLAIM", disposition: null, reason_codes: [], notes: null, corrections: [] })}
          onRelease={() => {
            if (hasUnsavedCorrections(rows)) setDialog("release");
            else void send({ action: "RELEASE", disposition: null, reason_codes: [], notes: null, corrections: [] });
          }}
          onApprove={() => setDialog("approve")}
          onCorrect={() => {
            setShowEditor(true);
            setRows((existing) => (existing.length > 0 ? existing : [emptyRow(newRowId())]));
          }}
          onReject={() => setDialog("reject")}
        />
      ) : null}

      {state.kind === "ready" && capabilities && showEditor ? (
        <form
          className={styles.panel}
          aria-label="Correction form"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            const result = validateCorrections(rows, capabilities.correction_policy);
            setValidation(result);
            const reasonMissing = correctReasons.length === 0;
            setFormError(reasonMissing ? "Select at least one reason code." : null);
            if (reasonMissing || !result.valid) {
              setInvalidAttempts((count) => count + 1);
              return;
            }
            setDialog("correct");
          }}
        >
          {validation && (!validation.valid || formError) ? (
            <div ref={summaryRef} tabIndex={-1} role="alert" className={`${styles.banner} ${styles.bannerError}`} data-testid="correction-error-summary">
              <p className={styles.bannerTitle}>Fix the following before submitting</p>
              <ul className={styles.bannerList}>
                {validation.formErrors.map((message) => <li key={message}>{message}</li>)}
                {formError ? <li>{formError}</li> : null}
                {Object.entries(validation.rowErrors).flatMap(([rowId, errs]) =>
                  Object.values(errs).map((message) => <li key={`${rowId}-${message}`}>Correction {rows.findIndex((row) => row.id === rowId) + 1}: {message}</li>),
                )}
              </ul>
            </div>
          ) : null}
          <CorrectionEditor
            ref={editorRef}
            policy={capabilities.correction_policy}
            rows={rows}
            onRowsChange={setRows}
            validation={validation}
            evidenceSuggestions={evidenceSuggestions}
            newRowId={newRowId}
          />
          <ReasonCodePicker
            legend="Reason codes"
            options={CORRECT_REASON_CODES}
            selected={correctReasons}
            onChange={setCorrectReasons}
            idPrefix="correct-reasons"
            error={validation && correctReasons.length === 0 ? "Select at least one reason code." : null}
          />
          <label className={styles.field}>
            Notes (optional)
            <textarea className={styles.input} rows={2} maxLength={4000} value={correctNotes} onChange={(event) => setCorrectNotes(event.target.value)} />
          </label>
          <div className={styles.actions}>
            <button type="submit" className={`${styles.button} ${styles.primary}`} disabled={locked}>Review and submit correction</button>
            <button
              type="button"
              className={styles.button}
              disabled={pending}
              onClick={() => {
                setShowEditor(false);
                setRows([]);
                setValidation(null);
              }}
            >
              Discard correction form
            </button>
          </div>
        </form>
      ) : null}

      {state.kind === "ready" && capabilities && frontendMode === "commit" && (capabilities.resume.disposition === "APPROVED" || capabilities.resume.disposition === "CORRECTED") ? (
        <ResumePanel
          operationsEnabled={props.operationsEnabled ?? false}
          capabilities={capabilities}
          eligible={state.actions.includes("RESUME_WORKFLOW")}
          locked={locked}
          reasons={resumeReasons}
          onReasonsChange={setResumeReasons}
          onResume={() => {
            const disposition = capabilities.resume.disposition;
            if (disposition !== "APPROVED" && disposition !== "CORRECTED") return;
            void send({ action: "RESUME_WORKFLOW", disposition, reason_codes: resumeReasons, notes: null, corrections: [] });
          }}
        />
      ) : null}

      <p className={styles.tertiary}>
        Every decision is append-only and audited. No payment, bank transfer or ERP posting can be started from this interface.
      </p>

      {/* Terminal-action confirmations */}
      <ConfirmDialog
        open={dialog === "approve"}
        title="Approve this invoice?"
        description={
          <>
            You are approving <strong>{invoiceLabel}</strong>. This records an append-only <strong>approved</strong> decision and resolves the review case. It cannot be edited or deleted afterwards.
          </>
        }
        confirmLabel="Approve invoice"
        pending={pending}
        confirmDisabled={approveReasons.length === 0}
        onCancel={() => setDialog(null)}
        onConfirm={() => {
          setDialog(null);
          void send({ action: "ACCEPT", disposition: "APPROVED", reason_codes: approveReasons, notes: approveNotes.trim() === "" ? null : approveNotes.trim(), corrections: [] });
        }}
      >
        <ReasonCodePicker legend="Reason codes (required)" options={APPROVE_REASON_CODES} selected={approveReasons} onChange={setApproveReasons} idPrefix="approve-reasons" />
        <label className={styles.field}>
          Notes (optional)
          <textarea className={styles.input} rows={2} maxLength={4000} value={approveNotes} onChange={(event) => setApproveNotes(event.target.value)} />
        </label>
      </ConfirmDialog>

      <ConfirmDialog
        open={dialog === "reject"}
        title="Reject this invoice?"
        description={
          <>
            Rejecting <strong>{invoiceLabel}</strong> is <strong>terminal for this review case</strong>: an append-only <strong>rejected</strong> decision is recorded, the case is closed, and the workflow cannot be resumed from it.
          </>
        }
        confirmLabel="Reject invoice"
        danger
        pending={pending}
        confirmDisabled={rejectReasons.length === 0 || rejectNotes.trim() === ""}
        onCancel={() => setDialog(null)}
        onConfirm={() => {
          setDialog(null);
          void send({ action: "REJECT", disposition: "REJECTED", reason_codes: rejectReasons, notes: rejectNotes.trim(), corrections: [] });
        }}
      >
        <ReasonCodePicker legend="Reason codes (required)" options={REJECT_REASON_CODES} selected={rejectReasons} onChange={setRejectReasons} idPrefix="reject-reasons" />
        <label className={styles.field}>
          Notes (required)
          <textarea className={styles.input} rows={3} maxLength={4000} required value={rejectNotes} onChange={(event) => setRejectNotes(event.target.value)} />
        </label>
      </ConfirmDialog>

      <ConfirmDialog
        open={dialog === "correct" && capabilities !== null}
        title="Submit these corrections?"
        description={
          <>
            This records an append-only <strong>corrected</strong> decision for <strong>{invoiceLabel}</strong> and resolves the review case. The original normalized invoice memory is not altered; the corrections become derived memory.
          </>
        }
        confirmLabel="Submit corrections"
        pending={pending}
        onCancel={() => setDialog(null)}
        onConfirm={() => {
          if (!capabilities) return;
          const corrections: CorrectionDraft[] | null = rowsToCorrections(rows, capabilities.correction_policy);
          setDialog(null);
          if (corrections === null) return;
          void send({ action: "CORRECT", disposition: "CORRECTED", reason_codes: correctReasons, notes: correctNotes.trim() === "" ? null : correctNotes.trim(), corrections });
        }}
      >
        <ul className={styles.summaryList} data-testid="correction-summary">
          {(capabilities ? (rowsToCorrections(rows, capabilities.correction_policy) ?? []) : []).map((correction) => (
            <li key={`${correction.field_name}-${correction.line_number ?? "h"}`}>
              {correction.line_number !== null ? `Line ${correction.line_number} ` : ""}{correction.field_name.replace(/_/g, " ").toLowerCase()}: {correction.previous_value ?? "not recorded"} → <strong>{correction.corrected_value}</strong> ({correction.evidence_reference_ids.length} evidence reference{correction.evidence_reference_ids.length === 1 ? "" : "s"})
            </li>
          ))}
        </ul>
      </ConfirmDialog>

      <ConfirmDialog
        open={dialog === "release"}
        title="Release and discard your correction draft?"
        description="You have an unsaved correction draft. Releasing this case returns it to the queue and discards that draft."
        confirmLabel="Release and discard draft"
        danger
        pending={pending}
        onCancel={() => setDialog(null)}
        onConfirm={() => {
          setDialog(null);
          setRows([]);
          setShowEditor(false);
          setValidation(null);
          void send({ action: "RELEASE", disposition: null, reason_codes: [], notes: null, corrections: [] });
        }}
      />
    </section>
  );
}

function ModeBanner({ state, frontendMode }: { state: WorkspaceState; frontendMode: string }) {
  if (state.kind === "disabled") {
    return (
      <div className={`${styles.banner} ${styles.bannerInfo}`} data-testid="mode-banner" data-mode="disabled">
        <p className={styles.bannerTitle}>Review actions are disabled</p>
        <p className={styles.bannerText}>This deployment is read-only. Claim, approve, correct, reject and resume controls are not available, and no command can be sent.</p>
      </div>
    );
  }
  if (state.kind === "misconfigured") {
    return (
      <div className={`${styles.banner} ${styles.bannerError}`} role="alert" data-testid="mode-banner" data-mode="misconfigured">
        <p className={styles.bannerTitle}>Command mode is misconfigured</p>
        <p className={styles.bannerText}>The frontend command-mode setting is invalid, so review actions are disabled. Ask an administrator to correct it.</p>
      </div>
    );
  }
  if (state.kind === "capabilities_unavailable") {
    return (
      <div className={`${styles.banner} ${styles.bannerWarn}`} data-testid="mode-banner" data-mode="unavailable">
        <p className={styles.bannerTitle}>Review actions are unavailable</p>
        <p className={styles.bannerText}>The available actions for this case could not be loaded, so no controls are shown. Reload the page to try again.</p>
      </div>
    );
  }
  if (state.kind === "mode_mismatch") {
    return (
      <div className={`${styles.banner} ${styles.bannerError}`} role="alert" data-testid="mode-banner" data-mode="mismatch">
        <p className={styles.bannerTitle}>Command modes do not match</p>
        <p className={styles.bannerText}>The frontend and the backend are configured for different command modes, so review actions are disabled. Ask an administrator to align them.</p>
      </div>
    );
  }
  if (state.kind === "no_permission") {
    return (
      <div className={`${styles.banner} ${styles.bannerInfo}`} data-testid="mode-banner" data-mode="no-permission">
        <p className={styles.bannerTitle}>Read-only access</p>
        <p className={styles.bannerText}>Your role does not permit any review action on this case.</p>
      </div>
    );
  }
  if (frontendMode === "validation_only") {
    return (
      <div className={`${styles.banner} ${styles.bannerWarn}`} data-testid="mode-banner" data-mode="validation_only">
        <p className={styles.bannerTitle}>Validation-only mode</p>
        <p className={styles.bannerText}>Commands are checked but <strong>never executed</strong>: no database changes are made, so the case status will not change. Workflow resume is unavailable.</p>
      </div>
    );
  }
  return (
    <div className={`${styles.banner} ${styles.bannerSuccess}`} data-testid="mode-banner" data-mode="commit">
      <p className={styles.bannerTitle}>Commit mode</p>
      <p className={styles.bannerText}>Actions are executed and recorded in the database as append-only decisions and audit events.</p>
    </div>
  );
}

function ReadyControls(props: {
  state: Extract<WorkspaceState, { kind: "ready" }>;
  capabilities: CommandCapabilitiesPayload;
  locked: boolean;
  pending: boolean;
  showEditor: boolean;
  hasUnsaved: boolean;
  onClaim: () => void;
  onRelease: () => void;
  onApprove: () => void;
  onCorrect: () => void;
  onReject: () => void;
}) {
  const { state, locked } = props;
  const has = (action: SupportedAction) => state.actions.includes(action);
  const primaryActions = (["CLAIM", "RELEASE", "ACCEPT", "CORRECT", "REJECT"] as const).filter(has);

  if (primaryActions.length === 0) {
    const { capabilities } = props;
    return (
      <p className={styles.note} data-testid="no-actions">
        {capabilities.assignment === "ASSIGNED_TO_OTHER"
          ? "This case is claimed by another reviewer. You cannot act on it."
          : capabilities.case_status === "RESOLVED" || capabilities.case_status === "REJECTED"
            ? "This case is resolved. Its decision is recorded below and cannot be changed."
            : "No actions are available for you on this case right now."}
      </p>
    );
  }

  return (
    <div className={styles.actions} role="group" aria-label="Review actions">
      {has("CLAIM") ? <button type="button" className={`${styles.button} ${styles.primary}`} disabled={locked} onClick={props.onClaim}>{ACTION_LABELS.CLAIM} this case</button> : null}
      {has("ACCEPT") ? <button type="button" className={`${styles.button} ${styles.primary}`} disabled={locked} onClick={props.onApprove}>{ACTION_LABELS.ACCEPT}</button> : null}
      {has("CORRECT") ? <button type="button" className={styles.button} disabled={locked} aria-expanded={props.showEditor} onClick={props.onCorrect}>{ACTION_LABELS.CORRECT}</button> : null}
      {has("REJECT") ? <button type="button" className={`${styles.button} ${styles.danger}`} disabled={locked} onClick={props.onReject}>{ACTION_LABELS.REJECT}</button> : null}
      {has("RELEASE") ? <button type="button" className={styles.button} disabled={locked} onClick={props.onRelease}>{ACTION_LABELS.RELEASE}</button> : null}
    </div>
  );
}

function ResumePanel(props: {
  operationsEnabled: boolean;
  capabilities: CommandCapabilitiesPayload;
  eligible: boolean;
  locked: boolean;
  reasons: string[];
  onReasonsChange: (next: string[]) => void;
  onResume: () => void;
}) {
  const { capabilities, eligible, locked } = props;
  const reason = capabilities.resume.ineligible_reason;
  return (
    <div className={styles.panel} data-testid="resume-panel">
      <h3 className={styles.legend}>Workflow resume</h3>
      {props.operationsEnabled ? (
        <p className={styles.note}>
          Requesting a resume records the restart stage and a derived version and <strong>queues a worker job</strong>. The request itself does not run the pipeline; follow the job below once it is queued.
        </p>
      ) : (
        <p className={styles.note}>
          Requesting a resume creates a <strong>controlled handoff only</strong>. It selects the restart stage and a derived version; it does not run the remaining pipeline.
        </p>
      )}
      {eligible ? (
        <>
          <ReasonCodePicker legend="Reason codes (required)" options={RESUME_REASON_CODES} selected={props.reasons} onChange={props.onReasonsChange} idPrefix="resume-reasons" />
          <div className={styles.actions}>
            <button type="button" className={`${styles.button} ${styles.primary}`} disabled={locked || props.reasons.length === 0} onClick={props.onResume}>
              {ACTION_LABELS.RESUME_WORKFLOW}
            </button>
          </div>
        </>
      ) : (
        <p className={styles.note} data-testid="resume-unavailable">
          {reason ? (RESUME_INELIGIBLE_COPY[reason] ?? "Workflow resume is not available for this case.") : "Workflow resume is not available for this case."}
        </p>
      )}
    </div>
  );
}
