import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AccountControls } from "@/components/application-shell/AccountControls";
import { AccountStateCard } from "@/components/feedback/AccountStateCard";
import { AUTH_REASONS } from "@/lib/auth/auth-reasons";

vi.mock("@clerk/nextjs", () => ({
  OrganizationSwitcher: () => <div data-testid="org-switcher" />,
  UserButton: () => <div data-testid="user-button" />,
  SignOutButton: ({ children, redirectUrl }: { children: React.ReactNode; redirectUrl?: string }) => (
    <span data-testid="sign-out-wrapper" data-redirect={redirectUrl}>
      {children}
    </span>
  ),
}));

describe("AccountStateCard", () => {
  it.each(AUTH_REASONS)("renders an accessible alert for %s", (reason) => {
    render(<AccountStateCard reason={reason} />);
    const card = screen.getByTestId("account-state");
    expect(card).toHaveAttribute("role", "alert");
    expect(card).toHaveAttribute("data-reason", reason);
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
  });

  it("explains an unmapped account and offers a way out, without identifiers", () => {
    render(<AccountStateCard reason="not-mapped" />);
    expect(screen.getByText(/not been registered/i)).toBeInTheDocument();
    expect(screen.getByTestId("account-retry")).toHaveAttribute("href", "/dashboard");
    expect(screen.getByTestId("account-sign-out")).toHaveTextContent("Sign out");
    expect(screen.getByTestId("org-switcher")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/user_|org_|sess_/);
  });

  it("tells an expired session to sign in again", () => {
    render(<AccountStateCard reason="session-expired" />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/session has expired/i);
    expect(screen.getByTestId("account-sign-out")).toHaveTextContent("Sign in again");
    expect(screen.getByTestId("sign-out-wrapper")).toHaveAttribute("data-redirect", "/sign-in");
  });

  it("explains an inactive membership", () => {
    render(<AccountStateCard reason="inactive" />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/access is inactive/i);
  });

  it("asks for an organization when none is active", () => {
    render(<AccountStateCard reason="no-organization" />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/choose an organization/i);
  });
});

describe("AccountControls", () => {
  it("shows the Clerk identity widgets and an explicit sign-out that returns to sign-in", () => {
    render(<AccountControls />);
    expect(screen.getByTestId("user-button")).toBeInTheDocument();
    expect(screen.getByTestId("org-switcher")).toBeInTheDocument();
    expect(screen.getByTestId("sign-out")).toHaveTextContent("Sign out");
    expect(screen.getByTestId("sign-out-wrapper")).toHaveAttribute("data-redirect", "/sign-in");
  });
});
