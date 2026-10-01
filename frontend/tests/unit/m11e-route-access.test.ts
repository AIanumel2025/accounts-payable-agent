import { describe, expect, it } from "vitest";
import { decideAccess, safeReturnPath } from "@/lib/auth/route-access";

const anonymous = { isSignedIn: false, hasOrganization: false };
const noOrganization = { isSignedIn: true, hasOrganization: false };
const member = { isSignedIn: true, hasOrganization: true };

describe("route access policy (server boundary)", () => {
  it.each(["/dashboard", "/review-queue", "/review-cases/abc", "/operations", "/operations/jobs/abc", "/"])(
    "redirects an unauthenticated visitor of %s to sign-in",
    (pathname) => {
      const decision = decideAccess({ pathname, ...anonymous });
      expect(decision.action).toBe("redirect");
      expect(decision).toMatchObject({ to: expect.stringMatching(/^\/sign-in\?redirect_url=/) });
    },
  );

  it("remembers where an unauthenticated visitor was going", () => {
    expect(decideAccess({ pathname: "/operations", returnTo: "/operations?page=2", ...anonymous })).toEqual({
      action: "redirect",
      to: "/sign-in?redirect_url=%2Foperations%3Fpage%3D2",
    });
  });

  it.each(["/dashboard", "/review-queue", "/review-cases/abc", "/operations", "/operations/jobs/abc"])(
    "requires an active organization for %s",
    (pathname) => {
      expect(decideAccess({ pathname, ...noOrganization })).toEqual({ action: "redirect", to: "/organization-required" });
    },
  );

  it("allows a member to reach application routes", () => {
    for (const pathname of ["/dashboard", "/review-queue", "/operations", "/review-cases/abc"]) {
      expect(decideAccess({ pathname, ...member })).toEqual({ action: "allow" });
    }
  });

  it("keeps sign-in and sign-up public", () => {
    for (const pathname of ["/sign-in", "/sign-in/factor-one", "/sign-up", "/sign-up/continue"]) {
      expect(decideAccess({ pathname, ...anonymous })).toEqual({ action: "allow" });
    }
  });

  it("lets a signed-in user without an organization reach only the account-state pages", () => {
    expect(decideAccess({ pathname: "/organization-required", ...noOrganization })).toEqual({ action: "allow" });
    expect(decideAccess({ pathname: "/access-denied", ...noOrganization })).toEqual({ action: "allow" });
    expect(decideAccess({ pathname: "/organization-required", ...anonymous }).action).toBe("redirect");
    expect(decideAccess({ pathname: "/access-denied", ...anonymous }).action).toBe("redirect");
  });

  it("answers API routes with JSON, never an HTML redirect", () => {
    for (const pathname of ["/api/backend/api/v1/dashboard", "/api/v1/operations/submissions", "/api/v1/review-cases/x/commands"]) {
      expect(decideAccess({ pathname, ...anonymous })).toEqual({ action: "json", status: 401, code: "AUTHENTICATION_REQUIRED" });
      expect(decideAccess({ pathname, ...noOrganization })).toEqual({ action: "json", status: 403, code: "ORGANIZATION_REQUIRED" });
      expect(decideAccess({ pathname, ...member })).toEqual({ action: "allow" });
    }
  });

  it("does not treat look-alike paths as public", () => {
    for (const pathname of ["/sign-in-evil", "/sign-input", "/signin", "/x/sign-in"]) {
      expect(decideAccess({ pathname, ...anonymous }).action).toBe("redirect");
    }
  });

  describe("safeReturnPath", () => {
    it.each([
      ["/operations?page=2", "/operations?page=2"],
      ["//evil.example", "/dashboard"],
      ["https://evil.example", "/dashboard"],
      ["/\\evil", "/dashboard"],
      ["", "/dashboard"],
      [undefined, "/dashboard"],
      ["/sign-in?x=1", "/dashboard"],
    ])("%s -> %s", (input, expected) => {
      expect(safeReturnPath(input as string | undefined)).toBe(expected);
    });
  });
});
