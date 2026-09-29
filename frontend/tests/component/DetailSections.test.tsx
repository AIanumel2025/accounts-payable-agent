import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EvidenceSection } from "@/components/review-detail/EvidenceSection";
import { FieldsSection } from "@/components/review-detail/FieldsSection";
import { IdentitySection } from "@/components/review-detail/IdentitySection";
import { LineMatchesSection } from "@/components/review-detail/LineMatchesSection";
import { MatchingSection } from "@/components/review-detail/MatchingSection";
import { ReviewDecisionsSection } from "@/components/review-detail/ReviewDecisionsSection";
import { TimelineSection } from "@/components/review-detail/TimelineSection";
import type {
  FinancialCheckPayload,
  InterfaceFieldValuePayload,
  InvoiceDetailPayload,
  LineMatchPayload,
  ReviewDecisionPayload,
  TimelineEventPayload,
} from "@/types/api-payloads";

const MISSING_FIELD: InterfaceFieldValuePayload = {
  field_name: "SUPPLIER_NAME",
  raw_value: null,
  normalized_value: null,
  value_type: "TEXT",
  confidence: null,
  review_required: true,
  evidence_reference_ids: [],
};

const PRESENT_FIELD: InterfaceFieldValuePayload = {
  field_name: "TOTAL_AMOUNT",
  raw_value: "50.10",
  normalized_value: "50.10",
  value_type: "DECIMAL",
  confidence: 0.6,
  review_required: false,
  evidence_reference_ids: ["ev-total-1", "ev-total-2"],
};

describe("FieldsSection (M11B task §8B)", () => {
  it("never converts a missing value to zero or a blank cell", () => {
    render(<FieldsSection fields={[MISSING_FIELD]} />);
    // the "—" missing-value marker, never "0" or "0.00"
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    expect(screen.queryByText("0.00")).not.toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("preserves the exact decimal text verbatim, never reformatted", () => {
    render(<FieldsSection fields={[{ ...PRESENT_FIELD, normalized_value: "873.580" }]} />);
    expect(screen.getByText("873.580")).toBeInTheDocument();
  });

  it("distinguishes a low-but-present confidence from a missing one", () => {
    render(<FieldsSection fields={[PRESENT_FIELD, MISSING_FIELD]} />);
    expect(screen.getByText("60%")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("renders an empty state rather than an empty table when there are no fields", () => {
    render(<FieldsSection fields={[]} />);
    expect(screen.getByText("No extracted fields")).toBeInTheDocument();
  });
});

describe("EvidenceSection (M11B task §8C/§21)", () => {
  it("renders original_document_uri as unavailable while it is null, never a local path", () => {
    render(
      <EvidenceSection sourceDocumentSha256={"a".repeat(64)} originalDocumentUri={null} fields={[PRESENT_FIELD]} financialChecks={[]} />,
    );
    expect(screen.getByText(/Not available/)).toBeInTheDocument();
    expect(screen.queryByText(/\/home\//)).not.toBeInTheDocument();
    expect(screen.queryByText(/^C:\\/)).not.toBeInTheDocument();
  });

  it("lists each field's evidence reference IDs as opaque tokens", () => {
    render(
      <EvidenceSection sourceDocumentSha256={"a".repeat(64)} originalDocumentUri={null} fields={[PRESENT_FIELD]} financialChecks={[]} />,
    );
    expect(screen.getByText("ev-total-1")).toBeInTheDocument();
    expect(screen.getByText("ev-total-2")).toBeInTheDocument();
  });

  it("shows a total evidence-reference count consistent with the field/check data", () => {
    render(
      <EvidenceSection sourceDocumentSha256={"a".repeat(64)} originalDocumentUri={null} fields={[PRESENT_FIELD]} financialChecks={[]} />,
    );
    expect(screen.getByText("2")).toBeInTheDocument();
  });
});

describe("MatchingSection (M11B task §8E: documented backend-contract limitation)", () => {
  it("documents that dedicated resolution statuses aren't on the detail endpoint, rather than inventing one", () => {
    render(<MatchingSection fields={[MISSING_FIELD]} />);
    expect(screen.getByText(/does not return dedicated supplier\/purchase-order\/goods-receipt resolution statuses/i)).toBeInTheDocument();
  });

  it("shows a missing supplier field as unavailable without a duplicated confidence suffix", () => {
    render(<MatchingSection fields={[MISSING_FIELD]} />);
    expect(screen.queryByText(/confidence/)).not.toBeInTheDocument();
  });
});

describe("LineMatchesSection (M11B task §8F)", () => {
  const LINE_MATCH: LineMatchPayload = {
    line_match_id: "line-match-1",
    invoice_line_number: 1,
    purchase_order_line_number: 1,
    description_status: "MATCHED",
    quantity_status: "MATCHED",
    unit_price_status: "MATCHED",
    line_total_status: "REVIEW_REQUIRED",
    review_required: true,
    review_reasons: ["INVOICE_LINE_TOTAL_MISSING"],
  };

  it("renders an empty state for zero line matches (e.g. purchase_order_status=NOT_REFERENCED), not a broken table", () => {
    render(<LineMatchesSection lineMatches={[]} />);
    expect(screen.getByText("No line matches recorded")).toBeInTheDocument();
  });

  it("renders each line match's per-column status and review reasons", () => {
    render(<LineMatchesSection lineMatches={[LINE_MATCH]} />);
    expect(screen.getByText("Invoice line total missing")).toBeInTheDocument();
  });
});

describe("TimelineSection (M11B task §8G)", () => {
  const EVENT: TimelineEventPayload = {
    event_id: "evt-1",
    event_type: "MEMORY_CREATED",
    stage: "INGESTION",
    status: "SUCCEEDED",
    actor_id: null,
    message: "Memory Created",
    occurred_at: "2026-06-01T09:15:00Z",
  };

  it("renders the real MemoryEventType label, not an invented one", () => {
    render(<TimelineSection timeline={[EVENT]} />);
    expect(screen.getByText("Memory record created")).toBeInTheDocument();
  });

  it("renders 'System' for a null actor_id rather than a blank", () => {
    render(<TimelineSection timeline={[EVENT]} />);
    expect(screen.getByText(/System/)).toBeInTheDocument();
  });

  it("preserves backend event order -- never re-sorted", () => {
    const later: TimelineEventPayload = { ...EVENT, event_id: "evt-2", event_type: "WORKFLOW_TRANSITIONED", occurred_at: "2026-06-01T09:20:00Z" };
    render(<TimelineSection timeline={[later, EVENT]} />);
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Workflow transitioned");
    expect(items[1]).toHaveTextContent("Memory record created");
  });
});

describe("ReviewDecisionsSection (M11B task §8H: read-only, no replay/edit/delete)", () => {
  it("renders an empty state for a fresh, undecided case", () => {
    render(<ReviewDecisionsSection decisions={[]} />);
    expect(screen.getByText("No review decisions recorded")).toBeInTheDocument();
  });

  it("renders a historical decision with no action buttons", () => {
    const decision: ReviewDecisionPayload = {
      decision_id: "11111111-1111-1111-1111-111111111111",
      reviewer_id: "reviewer-1",
      disposition: "APPROVED",
      reason_codes: [],
      notes: "Looks correct.",
      decided_at: "2026-06-02T10:00:00Z",
    };
    render(<ReviewDecisionsSection decisions={[decision]} />);
    expect(screen.getByText("Approved")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("IdentitySection (M11B task §8A)", () => {
  const DETAIL: Pick<
    InvoiceDetailPayload,
    "review_case_id" | "workflow_id" | "batch_id" | "document_id" | "source_name" | "workflow_status" | "current_stage" | "case_status" | "review_required" | "review_revision" | "review_reasons"
  > = {
    review_case_id: "11111111-1111-1111-1111-111111111111",
    workflow_id: "22222222-2222-2222-2222-222222222222",
    batch_id: "33333333-3333-3333-3333-333333333333",
    document_id: "44444444-4444-4444-4444-444444444444",
    source_name: "Template1_Instance90.jpg",
    workflow_status: "REVIEW_REQUIRED",
    current_stage: "HUMAN_REVIEW",
    case_status: "OPEN",
    review_required: true,
    review_revision: 1,
    review_reasons: ["SUPPLIER_NAME_MISSING"],
  };

  it("renders every identity field and the review reasons", () => {
    render(<IdentitySection detail={DETAIL as InvoiceDetailPayload} />);
    expect(screen.getByText("Template1_Instance90.jpg")).toBeInTheDocument();
    expect(screen.getByText("Supplier name missing")).toBeInTheDocument();
    expect(screen.getByText("Yes")).toBeInTheDocument();
  });
});
