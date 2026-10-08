import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ReviewActionWorkspace, type ReviewActionWorkspaceProps } from "@/components/review-actions/ReviewActionWorkspace";
import type { CommandOutcome, SubmitCommand } from "@/lib/commands/client";
import { parseCommandSuccess } from "@/lib/commands/contract";
import { deriveWorkspaceState } from "@/lib/commands/presentation";
import type { CommandCapabilitiesPayload } from "@/types/api-payloads";
import {
  CASE_ID, claimedCapabilities, executedEnvelope, makeCapabilities, resumeCapabilities, validatedEnvelope,
} from "../support/command-fixtures";

function ok(envelope: unknown): CommandOutcome {
  const success = parseCommandSuccess(envelope);
  if (!success) throw new Error("bad fixture");
  return { ok: true, success };
}
const fail = (errors: string[], httpStatus = 409, requestId: string | null = "77777777-7777-4777-8777-777777777777"): CommandOutcome => ({ ok: false, httpStatus, errors, requestId });

function setup(
  caps: CommandCapabilitiesPayload | null,
  options: { mode?: "disabled" | "validation_only" | "commit"; valid?: boolean; submit?: SubmitCommand; role?: string } = {},
) {
  const mode = options.mode ?? "commit";
  const submit = options.submit ?? vi.fn<SubmitCommand>(async () => ok(executedEnvelope()));
  const onRefresh = vi.fn();
  const build = (c: CommandCapabilitiesPayload | null): ReviewActionWorkspaceProps => ({
    reviewCaseId: CASE_ID,
    invoiceLabel: "INV-1001",
    csrfToken: "token",
    state: deriveWorkspaceState({ frontendMode: mode, modeValid: options.valid ?? true, capabilities: c }),
    capabilities: c,
    roleLabel: options.role ?? "AP reviewer",
    frontendMode: mode,
    evidenceSuggestions: { PURCHASE_ORDER_NUMBER: ["ev-po-1"] },
    onRefresh,
    submit,
  });
  const view = render(<ReviewActionWorkspace {...build(caps)} />);
  return { ...view, submit, onRefresh, rerenderWith: (c: CommandCapabilitiesPayload | null) => view.rerender(<ReviewActionWorkspace {...build(c)} />) };
}

const lastRequest = (submit: SubmitCommand) => (vi.mocked(submit).mock.calls.at(-1) as unknown as [string, string, Record<string, unknown>])[2];

describe("global states (task §19)", () => {
  it("disabled mode: banner only, no controls, no request", () => {
    setup(null, { mode: "disabled" });
    expect(screen.getByTestId("mode-banner")).toHaveAttribute("data-mode", "disabled");
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText(/no command can be sent/i)).toBeInTheDocument();
  });

  it("misconfigured mode: alert, no controls", () => {
    setup(null, { mode: "commit", valid: false });
    expect(screen.getByTestId("mode-banner")).toHaveAttribute("data-mode", "misconfigured");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("mode mismatch: alert, no controls", () => {
    setup(makeCapabilities({ command_mode: "VALIDATION_ONLY" }), { mode: "commit" });
    expect(screen.getByTestId("mode-banner")).toHaveAttribute("data-mode", "mismatch");
    expect(screen.queryByRole("button", { name: /claim/i })).toBeNull();
  });

  it("capabilities unavailable: explains and shows no controls", () => {
    setup(null, { mode: "commit" });
    expect(screen.getByTestId("mode-banner")).toHaveAttribute("data-mode", "unavailable");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("read-only role: no controls, clear message", () => {
    setup(makeCapabilities({ actor_role: "READ_ONLY_AUDITOR", permitted_actions: [], available_actions: [] }), { role: "Read-only auditor" });
    expect(screen.getByTestId("mode-banner")).toHaveAttribute("data-mode", "no-permission");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("validation-only mode shows the banner and never promises execution", () => {
    setup(makeCapabilities({ command_mode: "VALIDATION_ONLY" }), { mode: "validation_only" });
    expect(screen.getByTestId("mode-banner")).toHaveAttribute("data-mode", "validation_only");
    expect(screen.getByText(/never executed/i)).toBeInTheDocument();
  });

  it("commit mode shows the commit banner and the summary", () => {
    setup(makeCapabilities());
    expect(screen.getByTestId("mode-banner")).toHaveAttribute("data-mode", "commit");
    const summary = within(screen.getByTestId("action-summary"));
    expect(summary.getByText("Open")).toBeInTheDocument();
    expect(summary.getByText("Unassigned")).toBeInTheDocument();
    expect(summary.getByText("AP reviewer")).toBeInTheDocument();
  });
});

describe("control visibility by case state", () => {
  it("open case: only Claim", () => {
    setup(makeCapabilities());
    expect(screen.getByRole("button", { name: /claim this case/i })).toBeEnabled();
    for (const name of [/approve/i, /correct/i, /reject/i, /release/i]) expect(screen.queryByRole("button", { name: name })).toBeNull();
  });

  it("case claimed by the actor: Release, Approve, Correct, Reject", () => {
    setup(claimedCapabilities());
    for (const name of [/^approve$/i, /^correct$/i, /^reject$/i, /^release$/i]) expect(screen.getByRole("button", { name })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /claim/i })).toBeNull();
  });

  it("case claimed by someone else: no controls", () => {
    setup(makeCapabilities({ case_status: "IN_REVIEW", assignment: "ASSIGNED_TO_OTHER", available_actions: [] }));
    expect(screen.getByTestId("no-actions")).toHaveTextContent(/another reviewer/i);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("never renders a control for a deferred or payment action", () => {
    setup(claimedCapabilities());
    for (const name of [/supplier/i, /purchase order/i, /information/i, /escalate/i, /pay/i, /transfer/i, /erp/i]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("hides Approve and Reject for a role whose backend policy omits them", () => {
    const operator = claimedCapabilities({
      permitted_actions: ["CLAIM", "RELEASE", "CORRECT"],
      available_actions: claimedCapabilities().available_actions.filter((a) => a.action === "RELEASE" || a.action === "CORRECT"),
    });
    setup(operator);
    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();
    expect(screen.getByRole("button", { name: /^correct$/i })).toBeInTheDocument();
  });
});

describe("claim + pending + results", () => {
  it("claim sends the observed revisions and refreshes after a committed result", async () => {
    const { submit, onRefresh } = setup(makeCapabilities({ review_revision: 3, workflow_revision: 7 }));
    await userEvent.click(screen.getByRole("button", { name: /claim this case/i }));
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
    expect(submit).toHaveBeenCalledTimes(1);
    expect(lastRequest(submit)).toMatchObject({ action: "CLAIM", disposition: null, observed_review_revision: 3, observed_workflow_revision: 7, reason_codes: [], corrections: [] });
    expect(screen.getByTestId("command-result")).toHaveAttribute("data-result", "executed");
    expect(screen.getByText(/Executed — the database was updated and the action was recorded/)).toBeInTheDocument();
  });

  it("prevents a duplicate submission while pending (double click)", async () => {
    let release: (outcome: CommandOutcome) => void = () => {};
    const submit = vi.fn<SubmitCommand>(() => new Promise<CommandOutcome>((resolve) => { release = resolve; }));
    setup(makeCapabilities(), { submit });
    const button = screen.getByRole("button", { name: /claim this case/i });
    await userEvent.dblClick(button);
    expect(submit).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    expect(screen.getByText(/do not resubmit/i)).toBeInTheDocument();
    await act(async () => release(ok(executedEnvelope())));
    await waitFor(() => expect(screen.getByTestId("command-result")).toHaveAttribute("data-result", "executed"));
  });

  it("validation-only result says nothing changed and does not refresh", async () => {
    const submit = vi.fn<SubmitCommand>(async () => ok(validatedEnvelope()));
    const { onRefresh } = setup(makeCapabilities({ command_mode: "VALIDATION_ONLY" }), { mode: "validation_only", submit });
    await userEvent.click(screen.getByRole("button", { name: /claim this case/i }));
    const result = await screen.findByTestId("command-result");
    expect(result).toHaveAttribute("data-result", "validated");
    expect(within(result).getByText("Validated — no database changes were made.")).toBeInTheDocument();
    expect(result).toHaveTextContent(/not executed/i);
    expect(result).not.toHaveTextContent(/Executed —/);
    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.getByTestId("action-summary")).toHaveTextContent("Open"); // state is not pretended to change
  });

  it("idempotent result is worded as already recorded and still refreshes", async () => {
    const submit = vi.fn<SubmitCommand>(async () => ok({ ...executedEnvelope(), status: "IDEMPOTENT" }));
    const { onRefresh } = setup(makeCapabilities(), { submit });
    await userEvent.click(screen.getByRole("button", { name: /claim this case/i }));
    const result = await screen.findByTestId("command-result");
    expect(result).toHaveAttribute("data-result", "idempotent");
    expect(result).toHaveTextContent(/no duplicate was created/i);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("moves focus to the result region after a submission (task §21)", async () => {
    setup(makeCapabilities());
    await userEvent.click(screen.getByRole("button", { name: /claim this case/i }));
    const result = await screen.findByTestId("command-result");
    await waitFor(() => expect(result).toHaveFocus());
    expect(result).toHaveAttribute("aria-live", "polite");
  });
});

describe("conflicts and errors (task §17/§18)", () => {
  it.each([
    ["stale revision", ["STALE_REVIEW_REVISION"], /case changed/i],
    ["ownership conflict", ["CASE_NOT_OPEN", "CASE_ALREADY_ASSIGNED"], /Already claimed/i],
    ["idempotency conflict", ["IDEMPOTENCY_KEY_CONTENT_CONFLICT"], /Conflicting duplicate/i],
  ])("%s: alert with a safe message, refresh offered, controls locked until the data changes", async (_label, codes, message) => {
    const submit = vi.fn<SubmitCommand>(async () => fail(codes));
    const { onRefresh, rerenderWith } = setup(makeCapabilities(), { submit });
    await userEvent.click(screen.getByRole("button", { name: /claim this case/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(alert).toHaveTextContent("Support reference: 77777777-7777-4777-8777-777777777777");
    expect(alert).not.toHaveTextContent(/STALE_REVIEW_REVISION|CASE_NOT_OPEN|postgres|traceback/i);
    expect(screen.getByRole("button", { name: /claim this case/i })).toBeDisabled();
    expect(screen.getByTestId("refresh-required")).toBeInTheDocument();
    expect(onRefresh).not.toHaveBeenCalled(); // never silently retried / refreshed

    await userEvent.click(screen.getByRole("button", { name: /refresh case data/i }));
    expect(onRefresh).toHaveBeenCalledTimes(1);

    // Fresh server data (new revisions) unlocks the controls again.
    rerenderWith(makeCapabilities({ workflow_revision: 2 }));
    expect(screen.getByRole("button", { name: /claim this case/i })).toBeEnabled();
    expect(screen.queryByTestId("refresh-required")).toBeNull();
  });

  it("a transport failure keeps controls enabled so the identical request can be retried", async () => {
    const submit = vi.fn<SubmitCommand>()
      .mockResolvedValueOnce({ ok: false, httpStatus: null, errors: ["NETWORK_ERROR"], requestId: null })
      .mockResolvedValueOnce(ok(executedEnvelope()));
    setup(makeCapabilities(), { submit });
    await userEvent.click(screen.getByRole("button", { name: /claim this case/i }));
    await screen.findByRole("alert");
    const button = screen.getByRole("button", { name: /claim this case/i });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    // Identical retry => identical identity (task §8).
    const [first, second] = vi.mocked(submit).mock.calls.map((c) => c[2]);
    expect(second?.command_id).toBe(first?.command_id);
    expect(second?.idempotency_key).toBe(first?.idempotency_key);
  });

  it.each([
    ["backend unavailable", ["BACKEND_UNAVAILABLE"], 503, /could not be reached/i],
    ["database unavailable", ["DATABASE_UNAVAILABLE"], 503, /database is temporarily unavailable/i],
    ["mode mismatch", ["COMMAND_MODE_MISMATCH"], 409, /do not match/i],
    ["forbidden", ["ACTION_NOT_PERMITTED"], 403, /not permitted/i],
    ["not found", ["REVIEW_CASE_NOT_FOUND"], 404, /not found/i],
  ])("%s renders a controlled message", async (_label, codes, status, message) => {
    setup(makeCapabilities(), { submit: vi.fn<SubmitCommand>(async () => fail(codes, status)) });
    await userEvent.click(screen.getByRole("button", { name: /claim this case/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
  });
});

describe("approve (task §12)", () => {
  it("requires a deliberate confirmation with reason codes, and submits ACCEPT/APPROVED", async () => {
    const { submit } = setup(claimedCapabilities());
    await userEvent.click(screen.getByRole("button", { name: /^approve$/i }));

    const dialog = screen.getByRole("dialog", { name: /approve this invoice/i, hidden: false });
    expect(dialog).toHaveTextContent(/append-only/i);
    const confirm = within(dialog).getByRole("button", { name: /approve invoice/i });
    expect(confirm).toBeDisabled(); // reason codes required first
    expect(submit).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole("checkbox", { name: /totals verified/i }));
    await userEvent.type(within(dialog).getByLabelText(/notes/i), "Looks fine");
    await userEvent.click(confirm);

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(lastRequest(submit)).toMatchObject({
      action: "ACCEPT", disposition: "APPROVED", reason_codes: ["TOTALS_VERIFIED"], notes: "Looks fine", corrections: [],
      observed_review_revision: 1, observed_workflow_revision: 2,
    });
  });

  it("cancelling sends nothing", async () => {
    const { submit } = setup(claimedCapabilities());
    await userEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /cancel/i }));
    expect(submit).not.toHaveBeenCalled();
  });

  it("does not call a validated result 'approved'", async () => {
    const submit = vi.fn<SubmitCommand>(async () => ok({ ...validatedEnvelope(), data: { ...validatedEnvelope().data, action: "ACCEPT" } }));
    setup(claimedCapabilities({ command_mode: "VALIDATION_ONLY" }), { mode: "validation_only", submit });
    await userEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("checkbox", { name: /totals verified/i }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /approve invoice/i }));
    const result = await screen.findByTestId("command-result");
    expect(result).toHaveTextContent("Validated — no database changes were made.");
    expect(result).not.toHaveTextContent(/approved|executed —/i);
  });
});

describe("reject (task §14)", () => {
  it("needs reason codes and notes, warns it is terminal, submits REJECT/REJECTED", async () => {
    const { submit } = setup(claimedCapabilities());
    await userEvent.click(screen.getByRole("button", { name: /^reject$/i }));
    const dialog = screen.getByRole("dialog", { name: /reject this invoice/i });
    expect(dialog).toHaveTextContent(/terminal for this review case/i);
    const confirm = within(dialog).getByRole("button", { name: /reject invoice/i });
    expect(confirm).toBeDisabled();
    await userEvent.click(within(dialog).getByRole("checkbox", { name: /duplicate invoice/i }));
    expect(confirm).toBeDisabled(); // notes still required
    await userEvent.type(within(dialog).getByLabelText(/notes \(required\)/i), "Already paid last month");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(lastRequest(submit)).toMatchObject({ action: "REJECT", disposition: "REJECTED", reason_codes: ["DUPLICATE_INVOICE"], notes: "Already paid last month", corrections: [] });
  });
});

describe("correction editor (task §13)", () => {
  async function openEditor() {
    const utils = setup(claimedCapabilities());
    await userEvent.click(screen.getByRole("button", { name: /^correct$/i }));
    return utils;
  }

  it("shows the immutable original value and only backend-listed targets", async () => {
    await openEditor();
    const select = screen.getByLabelText(/field to correct/i);
    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["Select a field…", "Purchase Order Number (header)", "Total Amount (header)", "Line 1 — Description", "Line 1 — Quantity"]);
    await userEvent.selectOptions(select, "H:TOTAL_AMOUNT");
    expect(screen.getByTestId("correction-current-0")).toHaveTextContent("100.10");
    expect(screen.getByText(/original normalized invoice record is immutable/i)).toBeInTheDocument();
  });

  it("validates inline and moves focus to an error summary without discarding the form", async () => {
    await openEditor();
    await userEvent.selectOptions(screen.getByLabelText(/field to correct/i), "H:TOTAL_AMOUNT");
    await userEvent.type(screen.getByLabelText(/corrected value/i), "12.3.4");
    await userEvent.click(screen.getByRole("button", { name: /review and submit correction/i }));

    const summary = await screen.findByTestId("correction-error-summary");
    await waitFor(() => expect(summary).toHaveFocus());
    expect(summary).toHaveAttribute("role", "alert");
    expect(summary).toHaveTextContent(/exact decimal/i);
    expect(summary).toHaveTextContent(/meaningful reason/i);
    expect(summary).toHaveTextContent(/evidence/i);
    expect(screen.getByLabelText(/corrected value/i)).toHaveValue("12.3.4"); // preserved
    expect(screen.getByLabelText(/corrected value/i)).toHaveAttribute("aria-invalid", "true");
  });

  it("supports multiple corrections with evidence, then confirms and submits CORRECT/CORRECTED", async () => {
    const { submit } = await openEditor();
    const fill = async (index: number, target: string, value: string, evidence: string) => {
      const row = within(screen.getByTestId(`correction-row-${index}`));
      await userEvent.selectOptions(row.getByLabelText(/field to correct/i), target);
      await userEvent.type(row.getByLabelText(/corrected value/i), value);
      await userEvent.type(row.getByLabelText(/reason for this correction/i), "Checked against the source document");
      await userEvent.click(row.getByRole("checkbox", { name: new RegExp(evidence) }));
    };

    await fill(0, "H:TOTAL_AMOUNT", "100.100", "Total Amount"); // checkboxes are named by their label, not the UUID
    await userEvent.click(screen.getByRole("button", { name: /add another correction/i }));
    await fill(1, "L:1:LINE_QUANTITY", "6", "Purchase Order Number");
    await userEvent.click(screen.getByRole("checkbox", { name: /verified against evidence/i }));
    await userEvent.click(screen.getByRole("button", { name: /review and submit correction/i }));

    const dialog = await screen.findByRole("dialog", { name: /submit these corrections/i });
    const summary = within(dialog).getByTestId("correction-summary");
    expect(summary).toHaveTextContent("total amount: 100.10 → 100.100");
    expect(summary).toHaveTextContent("Line 1 line quantity: 5 → 6");
    expect(dialog).toHaveTextContent(/original normalized invoice memory is not altered/i);

    await userEvent.click(within(dialog).getByRole("button", { name: /submit corrections/i }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    const request = lastRequest(submit);
    expect(request).toMatchObject({ action: "CORRECT", disposition: "CORRECTED", reason_codes: ["EVIDENCE_VERIFIED"] });
    expect(request.corrections).toEqual([
      { field_name: "TOTAL_AMOUNT", line_number: null, previous_value: "100.10", corrected_value: "100.100", reason: "Checked against the source document", evidence_reference_ids: ["ev-total-1"] },
      { field_name: "LINE_QUANTITY", line_number: 1, previous_value: "5", corrected_value: "6", reason: "Checked against the source document", evidence_reference_ids: ["ev-po-1"] },
    ]);
  });

  it("only offers evidence references from the case and marks the field's own evidence", async () => {
    await openEditor();
    await userEvent.selectOptions(screen.getByLabelText(/field to correct/i), "H:PURCHASE_ORDER_NUMBER");
    const evidence = within(screen.getByRole("group", { name: /supporting evidence/i })).getAllByRole("checkbox");
    expect(evidence.map((box) => box.closest("label")?.textContent)).toEqual([
      "Extracted evidence — Purchase Order Number, page 1 (attached to this field) — PO 99",
      "Extracted evidence — Total Amount",
    ]);
  });

  it("lets a correction row be removed", async () => {
    await openEditor();
    await userEvent.click(screen.getByRole("button", { name: /remove correction 1/i }));
    expect(screen.queryByTestId("correction-row-0")).toBeNull();
  });

  it("asks before Release discards an unsaved correction draft", async () => {
    const { submit } = await openEditor();
    await userEvent.type(screen.getByLabelText(/reason for this correction/i), "half-written thought");
    await userEvent.click(screen.getByRole("button", { name: /^release$/i }));
    const dialog = screen.getByRole("dialog", { name: /discard your correction draft/i });
    expect(submit).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: /cancel/i }));
    expect(screen.getByLabelText(/reason for this correction/i)).toHaveValue("half-written thought");

    await userEvent.click(screen.getByRole("button", { name: /^release$/i }));
    await userEvent.click(within(screen.getByRole("dialog", { name: /discard your correction draft/i })).getByRole("button", { name: /release and discard draft/i }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(lastRequest(submit)).toMatchObject({ action: "RELEASE", disposition: null });
  });

  it("releases directly when there is no unsaved work", async () => {
    const { submit } = setup(claimedCapabilities());
    await userEvent.click(screen.getByRole("button", { name: /^release$/i }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps entered form content when a stale conflict comes back", async () => {
    const submit = vi.fn<SubmitCommand>(async () => fail(["STALE_WORKFLOW_REVISION"]));
    setup(claimedCapabilities(), { submit });
    await userEvent.click(screen.getByRole("button", { name: /^correct$/i }));
    const row = within(screen.getByTestId("correction-row-0"));
    await userEvent.selectOptions(row.getByLabelText(/field to correct/i), "H:PURCHASE_ORDER_NUMBER");
    await userEvent.type(row.getByLabelText(/corrected value/i), "99A");
    await userEvent.type(row.getByLabelText(/reason for this correction/i), "Verified PO number");
    await userEvent.click(row.getByRole("checkbox", { name: /Purchase Order Number/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /verified against evidence/i }));
    await userEvent.click(screen.getByRole("button", { name: /review and submit correction/i }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: /submit these corrections/i })).getByRole("button", { name: /submit corrections/i }));

    await screen.findByText(/This case changed/);
    expect(screen.getByLabelText(/corrected value/i)).toHaveValue("99A");
    expect(screen.getByLabelText(/reason for this correction/i)).toHaveValue("Verified PO number");
    expect(screen.getByRole("button", { name: /review and submit correction/i })).toBeDisabled();
  });
});

describe("workflow resume panel (task §15)", () => {
  it("approved decision: offers a controlled handoff and states nothing runs", async () => {
    const { submit } = setup(resumeCapabilities("APPROVED"));
    const panel = screen.getByTestId("resume-panel");
    expect(panel).toHaveTextContent(/controlled handoff only/i);
    await userEvent.click(within(panel).getByRole("button", { name: /request workflow resume/i }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(lastRequest(submit)).toMatchObject({
      action: "RESUME_WORKFLOW", disposition: "APPROVED", reason_codes: ["REVIEW_COMPLETED"], notes: null, corrections: [],
      observed_review_revision: 2, observed_workflow_revision: 3,
    });
  });

  it("submits the stored CORRECTED disposition", async () => {
    const { submit } = setup(resumeCapabilities("CORRECTED"));
    await userEvent.click(screen.getByRole("button", { name: /request workflow resume/i }));
    await waitFor(() => expect(submit).toHaveBeenCalled());
    expect(lastRequest(submit)).toMatchObject({ disposition: "CORRECTED", corrections: [] });
  });

  it("shows restart stage, derived version, decision and plan identity, and the handoff-only wording", async () => {
    const submit = vi.fn<SubmitCommand>(async () =>
      ok(executedEnvelope({
        workflow_resumed: true, resulting_case_status: "RESOLVED", decision_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        restart_stage: "REFERENCE_MATCHING", derived_version: "human-review-derived-0123456789abcdef", resume_plan_id: "33333333-3333-4333-8333-333333333333",
        message: "Workflow resume plan created successfully.",
      })),
    );
    setup(resumeCapabilities("CORRECTED"), { submit });
    await userEvent.click(screen.getByRole("button", { name: /request workflow resume/i }));
    const result = await screen.findByTestId("command-result");
    expect(result).toHaveTextContent("A controlled workflow-resume handoff was created. Downstream execution has not run yet.");
    expect(screen.getByTestId("resume-restart-stage")).toHaveTextContent("Reference matching");
    expect(screen.getByTestId("resume-derived-version")).toHaveTextContent("human-review-derived-0123456789abcdef");
    expect(result).toHaveTextContent("dddddddd-dddd-4ddd-8ddd-dddddddddddd");
    expect(result).toHaveTextContent("33333333-3333-4333-8333-333333333333");
  });

  it("is absent for a rejected decision and for validation-only mode", () => {
    setup(makeCapabilities({ case_status: "REJECTED", available_actions: [], resume: { eligible: false, already_requested: false, disposition: "REJECTED", decision_id: "x", ineligible_reason: "RESOLVED_CASE_REQUIRED" } }));
    expect(screen.queryByTestId("resume-panel")).toBeNull();
  });

  it("explains why resume is unavailable once already requested or for another reviewer", () => {
    setup(resumeCapabilities("APPROVED", {
      available_actions: [],
      resume: { eligible: false, already_requested: true, disposition: "APPROVED", decision_id: "x", ineligible_reason: "RESUME_ALREADY_REQUESTED" },
    }));
    expect(screen.getByTestId("resume-unavailable")).toHaveTextContent(/already created/i);
    expect(screen.queryByRole("button", { name: /request workflow resume/i })).toBeNull();
  });

  it("does not offer resume in validation-only mode", () => {
    setup(resumeCapabilities("APPROVED", { command_mode: "VALIDATION_ONLY" }), { mode: "validation_only" });
    expect(screen.queryByTestId("resume-panel")).toBeNull();
  });
});

describe("dialog accessibility (task §21)", () => {
  it("labels the dialog, focuses Cancel first, and restores focus to the opener on cancel", async () => {
    setup(claimedCapabilities());
    const opener = screen.getByRole("button", { name: /^approve$/i });
    opener.focus();
    await userEvent.click(opener);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAccessibleName(/approve this invoice/i);
    expect(dialog).toHaveAccessibleDescription(/append-only/i);
    expect(within(dialog).getByRole("button", { name: /cancel/i })).toHaveFocus();
    await userEvent.click(within(dialog).getByRole("button", { name: /cancel/i }));
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("groups reason codes in a fieldset with a legend", async () => {
    setup(claimedCapabilities());
    await userEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    expect(within(screen.getByRole("dialog")).getByRole("group", { name: /reason codes/i })).toBeInTheDocument();
  });
});
