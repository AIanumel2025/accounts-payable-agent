import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { JobDetailLive } from "@/components/operations/JobDetailLive";
import { ResumeJobPanel } from "@/components/operations/ResumeJobPanel";
import { CASE_ID, JOB_ID, NEW_CASE_ID, event, makeDetail, makeJob } from "../support/operations-fixtures";

const client = vi.hoisted(() => ({ submitInvoice: vi.fn(), fetchJobList: vi.fn(), fetchJobDetail: vi.fn() }));
vi.mock("@/lib/operations/client", () => client);

const STAGES = ["INGESTION", "PREPROCESSING", "OCR", "NORMALIZATION"];

describe("JobDetailLive", () => {
  beforeEach(() => {
    client.fetchJobDetail.mockReset();
    client.fetchJobList.mockReset();
  });

  it("shows a queued job with no result and no success claim", () => {
    render(<JobDetailLive initial={makeDetail({}, [event(1, "JOB_SUBMITTED", null, "queued", "QUEUED")])} pollIntervalMs={10_000} />);

    expect(screen.getByTestId("job-status")).toHaveTextContent("Queued");
    expect(screen.getByTestId("job-outcome")).toHaveTextContent(/waiting/i);
    expect(screen.queryByTestId("job-result")).not.toBeInTheDocument();
    expect(screen.queryByTestId("review-link")).not.toBeInTheDocument();
  });

  it("renders the ordered stage timeline with labels", () => {
    const events = [
      event(1, "JOB_SUBMITTED", null, "queued", "QUEUED"),
      ...STAGES.map((stage, index) => event(index + 2, "STAGE_STARTED", stage, `${stage} started.`)),
    ];
    render(<JobDetailLive initial={makeDetail({ status: "RUNNING", current_stage: "NORMALIZATION" }, events)} pollIntervalMs={10_000} />);

    const items = within(screen.getByTestId("job-timeline")).getAllByRole("listitem");
    expect(items).toHaveLength(5);
    expect(items[1]).toHaveTextContent("Stage started");
    expect(items[1]).toHaveTextContent("Ingestion");
    expect(items[4]).toHaveTextContent("Normalization");
    expect(screen.getByTestId("job-stage")).toHaveTextContent("Normalization");
  });

  it("links a review-required result to its review case and summarises it", () => {
    render(
      <JobDetailLive
        initial={makeDetail({
          status: "REVIEW_REQUIRED", review_case_id: CASE_ID,
          summary: { review_reasons: ["SUPPLIER_NAME_MISSING"], stages: [], executed_stages: [], corrected_fields: [], invoice_number: "INV-7", supplier_name: "Acme", currency: "EUR", total_amount: "10.50" },
        })}
        pollIntervalMs={10_000}
      />,
    );

    expect(within(screen.getByTestId("review-link")).getByRole("link")).toHaveAttribute("href", `/review-cases/${CASE_ID}`);
    const result = screen.getByTestId("job-result");
    expect(result).toHaveTextContent("INV-7");
    expect(result).toHaveTextContent("EUR 10.50");
    expect(result).toHaveTextContent("Supplier name missing");
  });

  it("shows a controlled failure with a next step and never the raw internals", () => {
    render(<JobDetailLive initial={makeDetail({ status: "FAILED", error_code: "ARTIFACT_HASH_MISMATCH" })} pollIntervalMs={10_000} />);

    const failure = screen.getByTestId("job-failure");
    expect(failure).toHaveTextContent(/failed verification/i);
    expect(failure).toHaveTextContent(/next step/i);
    expect(screen.queryByTestId("job-result")).not.toBeInTheDocument();
  });

  it("shows resume provenance: restart stage, executed stages and corrections", () => {
    render(
      <JobDetailLive
        initial={makeDetail({
          job_type: "RESUME_WORKFLOW", status: "SUCCEEDED", resumed_review_case_id: CASE_ID,
          summary: { review_reasons: [], stages: [], restart_stage: "FINANCIAL_VALIDATION", executed_stages: ["FINANCIAL_VALIDATION", "REFERENCE_MATCHING", "MEMORY_PERSISTENCE"], corrected_fields: ["TOTAL_AMOUNT"], derived_version: "human-review-derived-abc" },
        })}
        pollIntervalMs={10_000}
      />,
    );

    const provenance = screen.getByTestId("job-resume");
    expect(within(provenance).getByTestId("resume-stage")).toHaveTextContent("Financial validation");
    expect(within(provenance).getByTestId("resume-executed")).toHaveTextContent("Financial validation → Reference matching → Memory persistence");
    expect(provenance).toHaveTextContent("Total amount");
    expect(provenance).toHaveTextContent("human-review-derived-abc");
    expect(screen.getByTestId("resumed-from-link")).toHaveAttribute("href", `/review-cases/${CASE_ID}`);
  });

  it("polls a running job to completion, announces it once and stops", async () => {
    client.fetchJobDetail.mockResolvedValue({ ok: true, status: 200, data: makeDetail({ status: "SUCCEEDED" }, [event(1, "JOB_SUCCEEDED", null, "done", "SUCCEEDED")]) });
    render(<JobDetailLive initial={makeDetail({ status: "RUNNING" })} pollIntervalMs={30} />);

    await waitFor(() => expect(screen.getByTestId("job-status")).toHaveTextContent("Completed automatically"));
    expect(screen.getByTestId("job-announcer")).toHaveTextContent("Job Completed automatically.");
    const calls = client.fetchJobDetail.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(client.fetchJobDetail.mock.calls.length).toBe(calls);
    expect(client.fetchJobDetail).toHaveBeenCalledWith(JOB_ID);
  });
});

describe("ResumeJobPanel (review detail)", () => {
  const resume = (over = {}) => makeJob({ job_id: "44444444-4444-4444-8444-444444444444", job_type: "RESUME_WORKFLOW", resumed_review_case_id: CASE_ID, ...over });

  beforeEach(() => client.fetchJobList.mockReset());

  it("renders nothing without a related job", () => {
    const { container } = render(<ResumeJobPanel reviewCaseId={CASE_ID} initialJobs={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("links a case to the job that created it", () => {
    render(<ResumeJobPanel reviewCaseId={CASE_ID} initialJobs={[makeJob({ status: "REVIEW_REQUIRED", review_case_id: CASE_ID })]} />);
    expect(screen.getByTestId("origin-job-link")).toHaveAttribute("href", `/operations/jobs/${JOB_ID}`);
  });

  it("distinguishes queued, running, completed, returned to review and failed", () => {
    const cases: Array<[Parameters<typeof resume>[0], RegExp, string]> = [
      [{ status: "QUEUED" }, /has not resumed yet/i, "Queued"],
      [{ status: "RUNNING", current_stage: "REFERENCE_MATCHING" }, /worker is processing/i, "Running"],
      [{ status: "SUCCEEDED", summary: { review_reasons: [], stages: [], executed_stages: ["MEMORY_PERSISTENCE"], corrected_fields: [], restart_stage: "MEMORY_PERSISTENCE" } }, /completed/i, "Completed"],
      [{ status: "REVIEW_REQUIRED", review_case_id: NEW_CASE_ID }, /still require review/i, "Returned to review"],
      [{ status: "FAILED", error_code: "RESUME_OVERLAY_HASH_MISMATCH" }, /failed verification/i, "Failed"],
    ];

    for (const [over, text, label] of cases) {
      const { unmount } = render(<ResumeJobPanel reviewCaseId={CASE_ID} initialJobs={[resume(over)]} pollIntervalMs={10_000} />);
      const panel = screen.getByTestId("resume-job-panel");
      expect(within(panel).getByTestId("job-status")).toHaveTextContent(label);
      expect(panel).toHaveTextContent(text);
      unmount();
    }
  });

  it("calls onSettled exactly once when the resume job finishes", async () => {
    const onSettled = vi.fn();
    client.fetchJobList.mockResolvedValue({ ok: true, status: 200, data: { items: [resume({ status: "SUCCEEDED" })], pagination: {} } });
    render(<ResumeJobPanel reviewCaseId={CASE_ID} initialJobs={[resume({ status: "RUNNING" })]} pollIntervalMs={30} onSettled={onSettled} />);

    await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(onSettled).toHaveBeenCalledTimes(1);
  });

  it("offers the new review case when the resume returned to review", () => {
    render(<ResumeJobPanel reviewCaseId={CASE_ID} initialJobs={[resume({ status: "REVIEW_REQUIRED", review_case_id: NEW_CASE_ID })]} pollIntervalMs={10_000} />);
    expect(screen.getByTestId("new-review-link")).toHaveAttribute("href", `/review-cases/${NEW_CASE_ID}`);
  });
});
