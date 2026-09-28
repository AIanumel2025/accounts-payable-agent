import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DashboardBody } from "@/components/dashboard/DashboardBody";
import type { DashboardPayload } from "@/types/api-payloads";

const DASHBOARD: DashboardPayload = {
  tenant_id: "00000000-0000-0000-0000-000000000000",
  generated_at: "2026-06-15T12:00:00Z",
  total_invoices: 4,
  processing_invoices: 0,
  completed_invoices: 1,
  review_required_invoices: 3,
  failed_invoices: 0,
  open_review_cases: 3,
  unassigned_review_cases: 3,
  review_reason_counts: [
    { reason: "INHERITED_FINANCIAL_VALIDATION_REVIEW", count: 3 },
    { reason: "SUPPLIER_NAME_MISSING", count: 2 },
    { reason: "INVOICE_LINE_TOTAL_MISSING", count: 1 },
  ],
};

describe("DashboardBody", () => {
  it("renders metrics, review-reason analytics, and a last-refreshed timestamp together", () => {
    render(<DashboardBody dashboard={DASHBOARD} />);

    expect(screen.getByRole("heading", { name: "Processing overview" })).toBeInTheDocument();
    expect(screen.getByLabelText("Total invoices: 4")).toBeInTheDocument();

    expect(screen.getByRole("heading", { name: "Review reasons" })).toBeInTheDocument();
    expect(screen.getByText("Inherited financial validation review")).toBeInTheDocument();

    expect(screen.getByText(/Last refreshed/)).toBeInTheDocument();
  });
});
