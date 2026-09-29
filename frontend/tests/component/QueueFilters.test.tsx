import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

import { QueueFilters } from "@/components/review-queue/QueueFilters";
import type { ReviewQueueFilters } from "@/lib/api/review-queue-query";

describe("QueueFilters (M11B task §7: URL-backed filter state)", () => {
  beforeEach(() => {
    pushMock.mockReset();
  });

  it("only offers the four backend-supported status values, never AWAITING_INFORMATION/ESCALATED", () => {
    render(<QueueFilters filters={{ page: 1 }} />);
    const select = screen.getByLabelText("Status") as HTMLSelectElement;
    const values = Array.from(select.options).map((option) => option.value);
    expect(values).toEqual(["", "OPEN", "IN_REVIEW", "RESOLVED", "REJECTED"]);
  });

  it("navigates to a new URL with the selected status, resetting to page 1", async () => {
    render(<QueueFilters filters={{ page: 3 }} />);
    await userEvent.selectOptions(screen.getByLabelText("Status"), "OPEN");
    expect(pushMock).toHaveBeenCalledWith("/review-queue?status=OPEN");
  });

  it("disables 'Clear all filters' when no filter is active", () => {
    render(<QueueFilters filters={{ page: 1 }} />);
    expect(screen.getByRole("button", { name: "Clear all filters" })).toBeDisabled();
  });

  it("enables and clears filters back to the bare queue URL", async () => {
    render(<QueueFilters filters={{ page: 1, status: "OPEN", priority: "HIGH" }} />);
    const clearButton = screen.getByRole("button", { name: "Clear all filters" });
    expect(clearButton).toBeEnabled();
    await userEvent.click(clearButton);
    expect(pushMock).toHaveBeenCalledWith("/review-queue");
  });

  it("shows the live result count for assistive technology via role=status", () => {
    render(<QueueFilters filters={{ page: 1 } as ReviewQueueFilters} resultCount={3} />);
    expect(screen.getByRole("status")).toHaveTextContent("3 matching cases");
  });
});
