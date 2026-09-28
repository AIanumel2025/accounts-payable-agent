import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { workflowStatusLabel } from "@/lib/formatting/status";

describe("StatusBadge", () => {
  it("renders both the label text and a glyph, never colour alone", () => {
    render(<StatusBadge presentation={workflowStatusLabel("REVIEW_REQUIRED")} />);
    expect(screen.getByText("Review required")).toBeInTheDocument();
    // The glyph is a visible sibling node distinct from the text (aria-hidden, decorative).
    expect(document.querySelector('[aria-hidden="true"]')).toHaveTextContent("!");
  });
});
