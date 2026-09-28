import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const usePathnameMock = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => usePathnameMock(),
}));

import { NavList } from "@/components/application-shell/NavList";
import type { NavItem } from "@/components/application-shell/navigation";

const ITEMS: NavItem[] = [
  { label: "Dashboard", href: "/dashboard", status: "active", description: "" },
  { label: "Review queue", status: "forthcoming", description: "Coming in M11B" },
];

describe("NavList", () => {
  beforeEach(() => {
    usePathnameMock.mockReturnValue("/dashboard");
  });

  it("marks the current route active via aria-current", () => {
    render(<NavList items={ITEMS} />);
    const dashboardLink = screen.getByRole("link", { name: "Dashboard" });
    expect(dashboardLink).toHaveAttribute("aria-current", "page");
  });

  it("renders a forthcoming item as disabled, non-clickable text, never a link to the wrong page", () => {
    render(<NavList items={ITEMS} />);
    expect(screen.queryByRole("link", { name: /Review queue/ })).not.toBeInTheDocument();
    const disabledItem = screen.getByText("Review queue").closest("[aria-disabled]");
    expect(disabledItem).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("Coming in M11B")).toBeInTheDocument();
  });

  it("calls onNavigate when an active link is clicked (used to close the mobile drawer)", async () => {
    const onNavigate = vi.fn();
    render(<NavList items={ITEMS} onNavigate={onNavigate} />);
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(onNavigate).toHaveBeenCalledTimes(1);
  });
});
