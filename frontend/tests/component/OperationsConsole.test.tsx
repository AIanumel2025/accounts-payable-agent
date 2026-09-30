import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OperationsConsole } from "@/components/operations/OperationsConsole";
import { JOB_ID, makeJob } from "../support/operations-fixtures";

const client = vi.hoisted(() => ({ submitInvoice: vi.fn(), fetchJobList: vi.fn(), fetchJobDetail: vi.fn() }));
vi.mock("@/lib/operations/client", () => client);

const pdf = (name = "invoice-001.pdf") => new File([new Uint8Array([37, 80, 68, 70, 45, 1, 2, 3])], name, { type: "application/pdf" });

function renderConsole(jobs = [] as ReturnType<typeof makeJob>[]) {
  return render(<OperationsConsole initialJobs={jobs} csrfToken="token-1" pollIntervalMs={40} />);
}

describe("OperationsConsole", () => {
  beforeEach(() => {
    client.submitInvoice.mockReset();
    client.fetchJobList.mockReset();
    client.fetchJobList.mockResolvedValue({ ok: true, status: 200, data: { items: [], pagination: {} } });
  });

  it("starts empty with the action disabled until a file is chosen", () => {
    renderConsole();
    expect(screen.getByTestId("process-button")).toBeDisabled();
    expect(screen.getByText("No jobs yet")).toBeInTheDocument();
    expect(screen.getByLabelText(/choose a file/i)).toBeInTheDocument();
  });

  it("summarises the selected file and lets the user remove it", async () => {
    const user = userEvent.setup();
    renderConsole();

    await user.upload(screen.getByTestId("file-input"), pdf());
    expect(within(screen.getByTestId("selected-file")).getByText("invoice-001.pdf")).toBeInTheDocument();
    expect(screen.getByTestId("process-button")).toBeEnabled();
    expect(screen.getByTestId("operations-announcer")).toHaveTextContent("Selected invoice-001.pdf.");

    await user.click(screen.getByTestId("remove-file"));
    expect(screen.queryByTestId("selected-file")).not.toBeInTheDocument();
    expect(screen.getByTestId("process-button")).toBeDisabled();
  });

  it("explains an unsupported file in an alert and keeps submission disabled", async () => {
    const user = userEvent.setup({ applyAccept: false });
    renderConsole();

    await user.upload(screen.getByTestId("file-input"), new File([new Uint8Array([1])], "notes.txt", { type: "text/plain" }));

    expect(screen.getByRole("alert")).toHaveTextContent(/only pdf, png and jpeg/i);
    expect(screen.getByTestId("process-button")).toBeDisabled();
    expect(client.submitInvoice).not.toHaveBeenCalled();
  });

  it("queues the upload, says the worker has not finished, and lists the new job", async () => {
    client.submitInvoice.mockResolvedValue({ ok: true, status: 202, data: { idempotent_replay: false, job: makeJob() } });
    const user = userEvent.setup();
    renderConsole();

    await user.upload(screen.getByTestId("file-input"), pdf());
    await user.click(screen.getByTestId("process-button"));

    const notice = await screen.findByTestId("submission-result");
    expect(notice).toHaveAttribute("data-result", "queued");
    expect(notice).toHaveTextContent(/accepted and queued/i);
    expect(notice).toHaveTextContent(/has not finished/i);
    expect(within(notice).getByRole("link", { name: /follow this job/i })).toHaveAttribute("href", `/operations/jobs/${JOB_ID}`);
    expect(within(screen.getByTestId("job-table")).getByText("invoice-001.pdf")).toBeInTheDocument();
    expect(within(screen.getByTestId("job-table")).getByTestId("job-status")).toHaveTextContent("Queued");
    expect(client.submitInvoice.mock.calls[0]![1]).toBe("token-1");
  });

  it("maps a refusal to fixed copy and reuses the same operation id on an identical retry", async () => {
    client.submitInvoice.mockResolvedValue({ ok: false, status: 403, errors: ["ACTION_NOT_PERMITTED", "postgresql://u:p@h/db"] });
    const user = userEvent.setup();
    renderConsole();

    await user.upload(screen.getByTestId("file-input"), pdf());
    await user.click(screen.getByTestId("process-button"));

    const alert = await screen.findByTestId("submission-result");
    expect(alert).toHaveAttribute("data-result", "error");
    expect(alert).toHaveTextContent(/not permitted to submit/i);
    expect(alert).not.toHaveTextContent(/postgresql/);

    await user.click(screen.getByTestId("process-button"));
    await waitFor(() => expect(client.submitInvoice).toHaveBeenCalledTimes(2));
    expect(client.submitInvoice.mock.calls[1]![2]).toBe(client.submitInvoice.mock.calls[0]![2]);

    await user.upload(screen.getByTestId("file-input"), pdf("other.pdf"));
    await user.click(screen.getByTestId("process-button"));
    await waitFor(() => expect(client.submitInvoice).toHaveBeenCalledTimes(3));
    expect(client.submitInvoice.mock.calls[2]![2]).not.toBe(client.submitInvoice.mock.calls[0]![2]);
  });

  it("polls only while a job is active, announces the terminal change once, then stops", async () => {
    const running = makeJob({ status: "RUNNING", current_stage: "OCR" });
    const done = makeJob({ status: "REVIEW_REQUIRED", review_case_id: "22222222-2222-4222-8222-222222222222" });
    client.fetchJobList.mockResolvedValue({ ok: true, status: 200, data: { items: [done], pagination: {} } });

    renderConsole([running]);
    expect(screen.getByTestId("polling-note")).toBeInTheDocument();

    await waitFor(() => expect(screen.getByTestId("operations-announcer")).toHaveTextContent("invoice-001.pdf: Review required."));
    await waitFor(() => expect(screen.queryByTestId("polling-note")).not.toBeInTheDocument());
    const links = screen.getAllByRole("link", { name: /open review case/i, hidden: true });
    expect(links.length).toBeGreaterThan(0);
    for (const link of links) expect(link).toHaveAttribute("href", "/review-cases/22222222-2222-4222-8222-222222222222");

    const calls = client.fetchJobList.mock.calls.length;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 200));
    });
    expect(client.fetchJobList.mock.calls.length).toBe(calls); // stopped: nothing is active
  });

  it("refreshes on demand and keeps the last state with an alert when refresh fails", async () => {
    const user = userEvent.setup();
    renderConsole([makeJob({ status: "SUCCEEDED" })]);

    client.fetchJobList.mockResolvedValueOnce({ ok: false, status: null, errors: ["NETWORK_ERROR"] });
    await user.click(screen.getByTestId("refresh-jobs"));

    expect(await screen.findByTestId("list-error")).toHaveTextContent(/last known state/i);
    expect(within(screen.getByTestId("job-table")).getByText("invoice-001.pdf")).toBeInTheDocument();
  });

  it("never offers anything payment-shaped", () => {
    renderConsole();
    expect(document.body).not.toHaveTextContent(/pay invoice|execute payment|bank transfer|post to erp/i);
  });
});
