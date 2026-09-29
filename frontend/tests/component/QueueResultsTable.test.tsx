import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { QueueResultsTable } from "@/components/review-queue/QueueResultsTable";
import type { ReviewQueueItemPayload } from "@/types/api-payloads";

const ITEM_WITH_INVOICE_NUMBER: ReviewQueueItemPayload = {
  tenant_id: "00000000-0000-0000-0000-000000000000",
  review_case_id: "aaaaaaaa-0000-4000-8000-000000000001",
  workflow_id: "aaaaaaaa-0000-4000-8000-000000000011",
  batch_id: "aaaaaaaa-0000-4000-8000-0000000000b1",
  document_id: "aaaaaaaa-0000-4000-8000-000000000021",
  source_name: "invoice_Aaron Bergman_36258.pdf",
  invoice_number: "36258",
  supplier_name: null,
  currency: "USD",
  total_amount: "50.10",
  workflow_status: "REVIEW_REQUIRED",
  current_stage: "HUMAN_REVIEW",
  case_status: "IN_REVIEW",
  priority: "CRITICAL",
  review_reasons: ["SUPPLIER_NAME_MISSING", "INHERITED_FINANCIAL_VALIDATION_REVIEW"],
  supplier_status: "NOT_FOUND",
  purchase_order_status: "MATCHED",
  financial_validation_status: "REVIEW_REQUIRED",
  assigned_reviewer_id: "reviewer-1",
  review_revision: 2,
  created_at: "2026-06-01T09:15:00Z",
  updated_at: "2026-06-01T09:20:00Z",
};

const ITEM_WITHOUT_INVOICE_NUMBER: ReviewQueueItemPayload = {
  ...ITEM_WITH_INVOICE_NUMBER,
  review_case_id: "bbbbbbbb-0000-4000-8000-000000000002",
  source_name: "Template1_Instance90.jpg",
  invoice_number: null,
  assigned_reviewer_id: null,
  review_reasons: [],
};

describe("QueueResultsTable (M11B task §7/§13)", () => {
  it("uses invoice_number as the primary label when present, falling back to source_name", () => {
    render(<QueueResultsTable items={[ITEM_WITH_INVOICE_NUMBER, ITEM_WITHOUT_INVOICE_NUMBER]} returnTo="/review-queue" />);

    expect(screen.getAllByRole("link", { name: "Open review case for 36258" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: "Open review case for Template1_Instance90.jpg" }).length).toBeGreaterThan(0);
  });

  it("never uses the raw review_case_id UUID as a visible label", () => {
    render(<QueueResultsTable items={[ITEM_WITH_INVOICE_NUMBER]} returnTo="/review-queue" />);
    expect(screen.queryByText(ITEM_WITH_INVOICE_NUMBER.review_case_id)).not.toBeInTheDocument();
  });

  it("renders exactly one explicit detail link per item -- never a clickable <tr> (task §13)", () => {
    render(<QueueResultsTable items={[ITEM_WITH_INVOICE_NUMBER]} returnTo="/review-queue" />);
    const rows = screen.getAllByRole("row");
    // header row + 1 data row
    expect(rows).toHaveLength(2);
    const dataRow = rows[1]!;
    expect(dataRow.tagName).toBe("TR");
    // the <tr> itself carries no interactive role/handler; only its link does
    expect(dataRow).not.toHaveAttribute("onclick");
  });

  it("carries the current queue URL through to each detail link via ?from= (task §2: preserve filters on back-navigation)", () => {
    render(<QueueResultsTable items={[ITEM_WITH_INVOICE_NUMBER]} returnTo="/review-queue?priority=CRITICAL" />);
    const links = screen.getAllByRole("link", { name: "Open review case for 36258" });
    for (const link of links) {
      expect(link.getAttribute("href")).toContain(encodeURIComponent("/review-queue?priority=CRITICAL"));
    }
  });

  it("never hides review reasons on the mobile card presentation", () => {
    render(<QueueResultsTable items={[ITEM_WITH_INVOICE_NUMBER]} returnTo="/review-queue" />);
    const cardList = screen.getByTestId("queue-card-list");
    expect(cardList).toHaveTextContent("Supplier name missing");
    expect(cardList).toHaveTextContent("Inherited financial validation review");
  });

  it("renders 'None' for review reasons when the list is empty, never a blank cell", () => {
    render(<QueueResultsTable items={[ITEM_WITHOUT_INVOICE_NUMBER]} returnTo="/review-queue" />);
    expect(screen.getAllByText("None").length).toBeGreaterThan(0);
  });

  it("renders 'Unassigned' rather than blank for a null assigned_reviewer_id", () => {
    render(<QueueResultsTable items={[ITEM_WITHOUT_INVOICE_NUMBER]} returnTo="/review-queue" />);
    expect(screen.getAllByText("Unassigned").length).toBeGreaterThan(0);
  });
});
