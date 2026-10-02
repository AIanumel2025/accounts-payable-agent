import { describe, expect, it } from "vitest";
import { AUTH_REASONS, AUTH_REASON_PRESENTATION, authReasonFromCodes, isAuthReason } from "@/lib/auth/auth-reasons";

describe("authentication reasons", () => {
  it.each([
    [["TOKEN_EXPIRED"], 401, "session-expired"],
    [["ORGANIZATION_REQUIRED"], 401, "no-organization"],
    [["IDENTITY_NOT_MAPPED"], 403, "not-mapped"],
    [["TENANT_ACCESS_DENIED"], 403, "not-mapped"],
    [["IDENTITY_MEMBERSHIP_INACTIVE"], 403, "inactive"],
    [["AUTHENTICATION_UNAVAILABLE"], 503, "unavailable"],
    [["TOKEN_SIGNATURE_INVALID"], 401, "signed-out"],
    [["TOKEN_MISSING"], 401, "signed-out"],
    [["TOKEN_ISSUER_INVALID"], 401, "signed-out"],
    [[], 401, "signed-out"],
  ])("%j (HTTP %i) -> %s", (codes, status, expected) => {
    expect(authReasonFromCodes(codes, status)).toBe(expected);
  });

  it("does not reclassify ordinary permission or validation failures", () => {
    expect(authReasonFromCodes(["ACTION_NOT_PERMITTED"], 403)).toBeNull();
    expect(authReasonFromCodes(["REVIEW_CASE_NOT_FOUND"], 404)).toBeNull();
    expect(authReasonFromCodes([], 500)).toBeNull();
  });

  it("only accepts whitelisted reasons from the URL", () => {
    for (const reason of AUTH_REASONS) expect(isAuthReason(reason)).toBe(true);
    for (const value of ["", "admin", "<script>", null, undefined]) expect(isAuthReason(value)).toBe(false);
  });

  it("has fixed, identifier-free copy for every reason", () => {
    for (const reason of AUTH_REASONS) {
      const { title, message, actions } = AUTH_REASON_PRESENTATION[reason];
      expect(title.length).toBeGreaterThan(5);
      expect(message).not.toMatch(/user_|org_|sess_|Bearer|eyJ/);
      expect(actions.length).toBeGreaterThan(0);
    }
  });
});
