import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/dashboard",
}));

import { MobileNav } from "@/components/application-shell/MobileNav";

describe("MobileNav", () => {
  it("is collapsed by default and expands the drawer on toggle (keyboard-operable)", async () => {
    render(<MobileNav />);
    const toggle = screen.getByRole("button", { name: /open navigation/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();

    await userEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  });

  it("closes the drawer again when toggled a second time", async () => {
    render(<MobileNav />);
    const toggle = screen.getByRole("button");
    await userEvent.click(toggle);
    await userEvent.click(screen.getByRole("button", { name: /close navigation/i }));
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });
});
