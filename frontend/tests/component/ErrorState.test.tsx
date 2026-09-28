import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const refreshMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: refreshMock }),
}));

import { ErrorState } from "@/components/feedback/ErrorState";

describe("ErrorState", () => {
  it("shows a helpful, non-technical message for a retryable failure and offers retry", () => {
    render(<ErrorState kind="UNAVAILABLE" />);
    expect(screen.getByText("Backend unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // Never a stack trace, DSN, SQL, hostname, or filesystem path (task §10).
    expect(document.body.textContent).not.toMatch(/postgres|password|Traceback|at Object\.|\/home\/|\/var\//i);
  });

  it("does not offer retry for a non-retryable failure like misconfiguration", () => {
    render(<ErrorState kind="CONFIG_ERROR" />);
    expect(screen.getByText("Authentication is misconfigured")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("uses role=alert so assistive technology announces the error", () => {
    render(<ErrorState kind="FORBIDDEN" />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});
