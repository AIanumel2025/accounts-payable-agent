import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MetricGrid } from "@/components/dashboard/MetricGrid";
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
  review_reason_counts: [],
};

describe("MetricGrid", () => {
  it("renders every required metric with the controlled-fixture baseline values", () => {
    render(<MetricGrid dashboard={DASHBOARD} />);

    expect(screen.getByText("Total invoices")).toBeInTheDocument();
    expect(screen.getByLabelText("Total invoices: 4")).toBeInTheDocument();
    expect(screen.getByLabelText("Processing invoices: 0")).toBeInTheDocument();
    expect(screen.getByLabelText("Completed automatically: 1")).toBeInTheDocument();
    expect(screen.getByLabelText("Review required: 3")).toBeInTheDocument();
    expect(screen.getByLabelText("Failed invoices: 0")).toBeInTheDocument();
    expect(screen.getByLabelText("Open review cases: 3")).toBeInTheDocument();
    expect(screen.getByLabelText("Unassigned review cases: 3")).toBeInTheDocument();
  });

  it("exposes the metrics as a labelled list for assistive technology", () => {
    render(<MetricGrid dashboard={DASHBOARD} />);
    expect(screen.getByRole("list", { name: "Invoice processing metrics" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(7);
  });
});
