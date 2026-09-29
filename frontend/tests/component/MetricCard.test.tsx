import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MetricCard } from "@/components/dashboard/MetricCard";

describe("MetricCard (M11B task §11: dashboard-to-queue metric links)", () => {
  it("renders as a plain, non-interactive card when no href is given", () => {
    render(<MetricCard label="Total invoices" value={4} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("Total invoices")).toBeInTheDocument();
  });

  it("renders the whole card as one link to an exact, real backend filter when href is given", () => {
    render(<MetricCard label="Open review cases" value={3} href="/review-queue?status=OPEN" />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/review-queue?status=OPEN");
    expect(link).toHaveTextContent("Open review cases");
    expect(link).toHaveTextContent("3");
  });
});
