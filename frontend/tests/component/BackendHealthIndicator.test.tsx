import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BackendHealthIndicator } from "@/components/application-shell/BackendHealthIndicator";

const HEALTHY_INITIAL = {
  reachable: true,
  data: { service: "ap-agent-review-api", api_version: "0.1.0", command_mode: "VALIDATION_ONLY" as const, payment_execution: "PROHIBITED" as const },
  checkedAtIso: "2026-06-15T12:00:00Z",
};

const UNREACHABLE_INITIAL = { reachable: false, data: null, checkedAtIso: "2026-06-15T12:00:00Z" };

describe("BackendHealthIndicator", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the initial reachable state with label and glyph together", () => {
    render(<BackendHealthIndicator initial={HEALTHY_INITIAL} />);
    expect(screen.getByText("Backend connected")).toBeInTheDocument();
  });

  it("renders the initial unreachable state", () => {
    render(<BackendHealthIndicator initial={UNREACHABLE_INITIAL} />);
    expect(screen.getByText("Backend unavailable")).toBeInTheDocument();
  });

  it("recheck calls the server-side proxy route, never FastAPI directly from the browser", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ data: HEALTHY_INITIAL.data }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BackendHealthIndicator initial={UNREACHABLE_INITIAL} />);
    await userEvent.click(screen.getByTestId("backend-health-retry"));

    await waitFor(() => expect(screen.getByText("Backend connected")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith("/api/backend/health", { cache: "no-store" });
  });

  it("shows a stale note with last-connected time when a recheck fails after a prior success", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    render(<BackendHealthIndicator initial={HEALTHY_INITIAL} />);
    await userEvent.click(screen.getByTestId("backend-health-retry"));

    await waitFor(() => expect(screen.getByText("Backend unavailable")).toBeInTheDocument());
    expect(screen.getByTestId("backend-stale-note")).toBeInTheDocument();
  });
});
