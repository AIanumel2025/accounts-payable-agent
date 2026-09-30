import { describe, expect, it } from "vitest";
import { matchAllowedPath } from "@/app/api/backend/[...path]/route";

const VALID_UUID = "11111111-1111-1111-1111-111111111111";

describe("matchAllowedPath (M11B task §5)", () => {
  it("allows health with no query params", () => {
    expect(matchAllowedPath(["health"])).toEqual({ allowedQueryParams: new Set() });
  });

  it("allows the dashboard with only batch_id", () => {
    expect(matchAllowedPath(["api", "v1", "dashboard"])).toEqual({ allowedQueryParams: new Set(["batch_id"]) });
  });

  it("allows the review-cases list with its exact supported filters", () => {
    expect(matchAllowedPath(["api", "v1", "review-cases"])).toEqual({
      allowedQueryParams: new Set(["page", "page_size", "status", "assigned_to", "priority", "batch_id"]),
    });
  });

  it("allows a review-case detail path with a syntactically valid UUID", () => {
    expect(matchAllowedPath(["api", "v1", "review-cases", VALID_UUID])).toEqual({ allowedQueryParams: new Set() });
  });

  it("rejects a review-case detail path with a malformed UUID", () => {
    expect(matchAllowedPath(["api", "v1", "review-cases", "not-a-uuid"])).toBeNull();
    expect(matchAllowedPath(["api", "v1", "review-cases", "11111111-1111-1111-1111-11111111111"])).toBeNull(); // too short
  });

  it("rejects the review-command subpath even with a valid UUID -- never reachable through this proxy", () => {
    expect(matchAllowedPath(["api", "v1", "review-cases", VALID_UUID, "commands"])).toBeNull();
  });

  it("rejects an encoded-traversal-style segment (decodes to a literal '..' that matches nothing)", () => {
    expect(matchAllowedPath(["..", "commands"])).toBeNull();
    expect(matchAllowedPath(["api", "v1", "review-cases", "..", "commands"])).toBeNull();
    expect(matchAllowedPath(["api", "v1", "review-cases", VALID_UUID, ".."])).toBeNull();
  });

  it("rejects an empty path", () => {
    expect(matchAllowedPath([])).toBeNull();
  });

  it("rejects a path containing an empty segment", () => {
    expect(matchAllowedPath(["api", "", "dashboard"])).toBeNull();
  });

  it("rejects an unrelated path", () => {
    expect(matchAllowedPath(["api", "v1", "something-else"])).toBeNull();
  });

  it("rejects a path that is a prefix of an allowed one", () => {
    expect(matchAllowedPath(["api", "v1"])).toBeNull();
    expect(matchAllowedPath(["api"])).toBeNull();
  });

  // M11D Core: read-only operations paths (exact shapes, gated by the operations mode in the GET handler).
  it("allows the job list with its supported filters, flagged as an operations path", () => {
    expect(matchAllowedPath(["api", "v1", "operations", "jobs"])).toEqual({
      allowedQueryParams: new Set(["page", "page_size", "status", "job_type", "review_case_id"]),
      requiresOperations: true,
    });
  });

  it("allows a job detail path with a valid UUID only", () => {
    expect(matchAllowedPath(["api", "v1", "operations", "jobs", VALID_UUID])).toEqual({
      allowedQueryParams: new Set(),
      requiresOperations: true,
    });
    expect(matchAllowedPath(["api", "v1", "operations", "jobs", "not-a-uuid"])).toBeNull();
    expect(matchAllowedPath(["api", "v1", "operations", "jobs", ".."])).toBeNull();
  });

  it("never exposes the submission endpoint or job sub-paths through the read-only proxy", () => {
    expect(matchAllowedPath(["api", "v1", "operations", "submissions"])).toBeNull();
    expect(matchAllowedPath(["api", "v1", "operations"])).toBeNull();
    expect(matchAllowedPath(["api", "v1", "operations", "jobs", VALID_UUID, "events"])).toBeNull();
    expect(matchAllowedPath(["api", "v1", "operations", "jobs", VALID_UUID, "cancel"])).toBeNull();
  });
});
