import { describe, expect, it } from "vitest";
import { errorsNeedRefresh, isSafeToRetry, presentErrorCodes } from "@/lib/commands/messages";

describe("error presentation (task §18)", () => {
  it.each([
    "STALE_REVIEW_REVISION", "STALE_WORKFLOW_REVISION", "CASE_NOT_OPEN", "CASE_ALREADY_ASSIGNED", "CASE_ASSIGNED_TO_DIFFERENT_REVIEWER",
    "IDEMPOTENCY_KEY_CONTENT_CONFLICT", "WORKFLOW_NOT_AWAITING_RESUME", "DATABASE_UNAVAILABLE",
  ])("%s requires a refresh and is never silently retried", (code) => {
    expect(errorsNeedRefresh([code])).toBe(true);
  });

  it("maps validation codes to their own copy, without a refresh", () => {
    for (const code of ["CORRECTION_REASON_REQUIRED", "CORRECTION_EVIDENCE_REQUIRED", "NOTES_REQUIRED", "REASON_CODE_REQUIRED"]) {
      expect(errorsNeedRefresh([code])).toBe(false);
    }
  });

  it("falls back to generic copy for unknown codes and never leaks the raw code", () => {
    const [presentation] = presentErrorCodes(["SOMETHING_INTERNAL_XYZ"]);
    expect(presentation?.title).toBeTruthy();
    expect(JSON.stringify(presentation)).not.toContain("SOMETHING_INTERNAL_XYZ");
  });

  it("gives field-level codes a friendly sentence", () => {
    expect(presentErrorCodes(["REQUEST_VALIDATION_FAILED", "INVALID_FIELD:corrections.0.reason"]).length).toBeGreaterThan(0);
  });

  it("deduplicates and never emits SQL, paths or DSNs", () => {
    const text = JSON.stringify(presentErrorCodes(["DATABASE_UNAVAILABLE", "INTERNAL_ERROR", "BACKEND_UNAVAILABLE", "BACKEND_TIMEOUT", "MALFORMED_RESPONSE"]));
    expect(text).not.toMatch(/postgres|SELECT |\/home\/|Traceback|psycopg/i);
  });

  it("marks only transport-level failures as safe to retry verbatim", () => {
    expect(isSafeToRetry(null, ["NETWORK_ERROR"])).toBe(true);
    expect(isSafeToRetry(503, ["BACKEND_UNAVAILABLE"])).toBe(true);
    expect(isSafeToRetry(503, ["BACKEND_TIMEOUT"])).toBe(true);
    expect(isSafeToRetry(409, ["STALE_REVIEW_REVISION"])).toBe(false);
  });
});
