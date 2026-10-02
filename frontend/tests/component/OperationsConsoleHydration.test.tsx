import { act, screen, waitFor } from "@testing-library/react";
import { hydrateRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OperationsConsole } from "@/components/operations/OperationsConsole";

const client = vi.hoisted(() => ({ submitInvoice: vi.fn(), fetchJobList: vi.fn(), fetchJobDetail: vi.fn() }));
vi.mock("@/lib/operations/client", () => client);

/**
 * Regression (M11D known issue, fixed in M11E): choosing a file before React
 * hydrated was silently lost, because the server-rendered input had no
 * handler yet. The input must now be unavailable until it is functional, and a
 * selection that nevertheless reaches the DOM early must be preserved.
 */

const pdf = new File([new Uint8Array([37, 80, 68, 70, 45, 1, 2, 3])], "early-choice.pdf", { type: "application/pdf" });

function serverRenderedContainer(): HTMLDivElement {
  const container = document.createElement("div");
  document.body.appendChild(container);
  container.innerHTML = renderToString(<OperationsConsole initialJobs={[]} csrfToken="token-1" pollIntervalMs={40} />);
  return container;
}

describe("OperationsConsole before and after hydration", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    client.fetchJobList.mockReset();
    client.fetchJobList.mockResolvedValue({ ok: true, status: 200, data: { items: [], pagination: {} } });
  });

  it("renders the file input disabled in the server HTML, so nothing can be chosen against an inert control", () => {
    const input = serverRenderedContainer().querySelector<HTMLInputElement>('[data-testid="file-input"]');
    expect(input).not.toBeNull();
    expect(input!.disabled).toBe(true);
  });

  it("enables the input once hydration has finished", async () => {
    const container = serverRenderedContainer();
    await act(async () => {
      hydrateRoot(container, <OperationsConsole initialJobs={[]} csrfToken="token-1" pollIntervalMs={40} />);
    });

    await waitFor(() => expect(screen.getByTestId("file-input")).toBeEnabled());
  });

  it("keeps a file that reached the input before hydration", async () => {
    const container = serverRenderedContainer();
    const input = container.querySelector<HTMLInputElement>('[data-testid="file-input"]')!;
    // What a browser (or an automation tool) leaves behind when a file is chosen early.
    Object.defineProperty(input, "files", { configurable: true, value: Object.assign([pdf], { item: (index: number) => [pdf][index] ?? null }) });

    await act(async () => {
      hydrateRoot(container, <OperationsConsole initialJobs={[]} csrfToken="token-1" pollIntervalMs={40} />);
    });

    await waitFor(() => expect(screen.getByTestId("selected-file")).toHaveTextContent("early-choice.pdf"));
    expect(screen.getByTestId("process-button")).toBeEnabled();
    expect(screen.getByTestId("operations-announcer")).toHaveTextContent("Selected early-choice.pdf.");
  });
});
