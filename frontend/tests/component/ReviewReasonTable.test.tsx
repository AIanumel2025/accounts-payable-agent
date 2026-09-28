import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ReviewReasonTable } from "@/components/dashboard/ReviewReasonTable";

describe("ReviewReasonTable", () => {
  it("renders the controlled-fixture review-reason analytics, sorted by count descending", () => {
    render(
      <ReviewReasonTable
        reasons={[
          { reason: "INVOICE_LINE_TOTAL_MISSING", count: 1 },
          { reason: "INHERITED_FINANCIAL_VALIDATION_REVIEW", count: 3 },
          { reason: "SUPPLIER_NAME_MISSING", count: 2 },
        ]}
      />,
    );

    const rows = screen.getAllByRole("row").slice(1); // skip header row
    expect(rows[0]).toHaveTextContent("Inherited financial validation review");
    expect(rows[0]).toHaveTextContent("3");
    expect(rows[1]).toHaveTextContent("Supplier name missing");
    expect(rows[2]).toHaveTextContent("Invoice line total missing");
  });

  it("renders an empty state, not an empty table, when there are no reasons", () => {
    render(<ReviewReasonTable reasons={[]} />);
    expect(screen.getByTestId("empty-state")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("uses real table semantics with a caption and column headers", () => {
    render(<ReviewReasonTable reasons={[{ reason: "SUPPLIER_NAME_MISSING", count: 2 }]} />);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Reason" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Count" })).toBeInTheDocument();
  });
});
