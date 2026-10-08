import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CorrectionEditor } from "@/components/review-actions/CorrectionEditor";
import { parseCapabilities } from "@/lib/commands/capabilities";
import { emptyRow, type CorrectionRow } from "@/lib/commands/corrections";
import { makeCapabilities } from "../support/command-fixtures";

const SOURCE_ID = "0b3a3d52-6f0e-5b43-9d6e-2f6a7c1f4c11";

function policy() {
  const base = makeCapabilities();
  const raw = {
    ...base,
    correction_policy: {
      ...base.correction_policy,
      header_fields: [{ field_name: "SUPPLIER_NAME", current_value: null }, ...base.correction_policy.header_fields],
      evidence_reference_ids: [SOURCE_ID, "ev-po-1"],
      evidence_options: [
        { reference_id: SOURCE_ID, evidence_type: "SOURCE_DOCUMENT", label: "Original source invoice — SHA-256 verified", page_number: null, snippet: null },
        { reference_id: "ev-po-1", evidence_type: "EXTRACTED_FIELD", label: "Extracted evidence — Purchase Order Number, page 1", page_number: 1, snippet: "PO 99" },
      ],
    },
  };
  return parseCapabilities(raw)!.correction_policy;
}

describe("CorrectionEditor evidence selector (M11E.5)", () => {
  it("shows a human-readable label for the source invoice instead of an unexplained id", () => {
    const rows: CorrectionRow[] = [{ ...emptyRow("r1"), target: "H:SUPPLIER_NAME" }];
    render(<CorrectionEditor policy={policy()} rows={rows} onRowsChange={() => {}} validation={null} evidenceSuggestions={{}} newRowId={() => "n"} />);

    const evidence = screen.getByRole("group", { name: "Supporting evidence" });
    expect(within(evidence).getByText("Original source invoice — SHA-256 verified")).toBeInTheDocument();
    expect(within(evidence).getByText("Extracted evidence — Purchase Order Number, page 1")).toBeInTheDocument();
    expect(within(evidence).getByText("PO 99")).toBeInTheDocument();
    expect(evidence.textContent ?? "").not.toContain(SOURCE_ID); // the opaque id is never the visible text
  });

  it("lists the original source invoice before other evidence when nothing is attached to the field", () => {
    const rows: CorrectionRow[] = [{ ...emptyRow("r1"), target: "H:SUPPLIER_NAME" }];
    render(<CorrectionEditor policy={policy()} rows={rows} onRowsChange={() => {}} validation={null} evidenceSuggestions={{}} newRowId={() => "n"} />);

    const boxes = within(screen.getByRole("group", { name: "Supporting evidence" })).getAllByRole("checkbox");
    expect(boxes.map((box) => box.closest("label")?.getAttribute("data-evidence-type"))).toEqual(["SOURCE_DOCUMENT", "EXTRACTED_FIELD"]);
  });

  it("selects the source invoice by its reference id only", () => {
    const onRowsChange = vi.fn();
    const rows: CorrectionRow[] = [{ ...emptyRow("r1"), target: "H:SUPPLIER_NAME" }];
    render(<CorrectionEditor policy={policy()} rows={rows} onRowsChange={onRowsChange} validation={null} evidenceSuggestions={{}} newRowId={() => "n"} />);

    fireEvent.click(screen.getByLabelText(/Original source invoice/));
    expect(onRowsChange).toHaveBeenCalledWith([expect.objectContaining({ evidenceIds: [SOURCE_ID] })]);
  });

  it("offers the missing supplier as an editable target", () => {
    const rows: CorrectionRow[] = [{ ...emptyRow("r1"), target: "" }];
    render(<CorrectionEditor policy={policy()} rows={rows} onRowsChange={() => {}} validation={null} evidenceSuggestions={{}} newRowId={() => "n"} />);

    expect(screen.getByRole("option", { name: "Supplier Name (header)" })).toBeInTheDocument();
  });
});
