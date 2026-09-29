import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Breadcrumb } from "@/components/application-shell/Breadcrumb";

describe("Breadcrumb (M11B task §2/§8: detail-page breadcrumb navigation back)", () => {
  it("renders every ancestor as a real link and the current page as non-link text", () => {
    render(<Breadcrumb trail={[{ label: "Review queue", href: "/review-queue?priority=HIGH" }]} current="36258" />);

    const link = screen.getByRole("link", { name: "Review queue" });
    expect(link).toHaveAttribute("href", "/review-queue?priority=HIGH");

    const current = screen.getByText("36258");
    expect(current.tagName).not.toBe("A");
    expect(current).toHaveAttribute("aria-current", "page");
  });

  it("exposes itself as a labelled navigation landmark", () => {
    render(<Breadcrumb trail={[{ label: "Review queue", href: "/review-queue" }]} current="Review case" />);
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toBeInTheDocument();
  });
});
