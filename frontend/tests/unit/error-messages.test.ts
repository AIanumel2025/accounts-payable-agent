import { describe, expect, it } from "vitest";
import type { AppErrorKind } from "@/lib/api/dashboard";
import { presentError } from "@/lib/api/error-messages";

const ALL_KINDS: AppErrorKind[] = [
  "CONFIG_ERROR",
  "UNAUTHORIZED",
  "FORBIDDEN",
  "NOT_FOUND",
  "TIMEOUT",
  "UNAVAILABLE",
  "MALFORMED_RESPONSE",
  "UNKNOWN",
];

describe("presentError", () => {
  it("has a presentation for every error kind, none of which leak implementation detail", () => {
    for (const kind of ALL_KINDS) {
      const presentation = presentError(kind);
      expect(presentation.title.length).toBeGreaterThan(0);
      expect(presentation.description.length).toBeGreaterThan(0);
      expect(presentation.description).not.toMatch(/postgres|dsn|password|traceback|sql|\/home\/|\/var\//i);
    }
  });

  it("marks configuration and permission failures as non-retryable", () => {
    expect(presentError("CONFIG_ERROR").retryable).toBe(false);
    expect(presentError("UNAUTHORIZED").retryable).toBe(false);
    expect(presentError("FORBIDDEN").retryable).toBe(false);
  });

  it("marks transient failures as retryable", () => {
    expect(presentError("TIMEOUT").retryable).toBe(true);
    expect(presentError("UNAVAILABLE").retryable).toBe(true);
  });
});
