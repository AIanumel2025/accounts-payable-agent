import { describe, expect, it } from "vitest";
import {
  DEFAULT_QUEUE_FILTERS,
  clearedFilters,
  filtersToBackendParams,
  filtersToUrlSearchParams,
  hasActiveFilters,
  parseQueueFiltersFromSearchParams,
  parseSafeReturnTo,
  SUPPORTED_QUEUE_PRIORITY_FILTERS,
  SUPPORTED_QUEUE_STATUS_FILTERS,
} from "@/lib/api/review-queue-query";

describe("SUPPORTED_QUEUE_STATUS_FILTERS", () => {
  it("only exposes the four status values the backend actually supports as a filter", () => {
    expect(SUPPORTED_QUEUE_STATUS_FILTERS).toEqual(["OPEN", "IN_REVIEW", "RESOLVED", "REJECTED"]);
    expect(SUPPORTED_QUEUE_STATUS_FILTERS).not.toContain("AWAITING_INFORMATION");
    expect(SUPPORTED_QUEUE_STATUS_FILTERS).not.toContain("ESCALATED");
  });
});

describe("filtersToBackendParams", () => {
  it("always includes page, and only includes keys that are set", () => {
    expect(filtersToBackendParams(DEFAULT_QUEUE_FILTERS)).toEqual({ page: "1" });
  });

  it("uses the exact FastAPI query-parameter names", () => {
    const params = filtersToBackendParams({
      page: 2,
      pageSize: 10,
      status: "OPEN",
      assignedTo: "reviewer-1",
      priority: "HIGH",
      batchId: "11111111-1111-1111-1111-111111111111",
    });
    expect(params).toEqual({
      page: "2",
      page_size: "10",
      status: "OPEN",
      assigned_to: "reviewer-1",
      priority: "HIGH",
      batch_id: "11111111-1111-1111-1111-111111111111",
    });
  });
});

describe("filtersToUrlSearchParams / parseQueueFiltersFromSearchParams round-trip", () => {
  it("round-trips a full filter set through the URL", () => {
    const original = {
      page: 3,
      pageSize: 5,
      status: "IN_REVIEW" as const,
      assignedTo: "reviewer-2",
      priority: "CRITICAL" as const,
      batchId: "22222222-2222-2222-2222-222222222222",
    };
    const url = filtersToUrlSearchParams(original);
    const { filters, warnings } = parseQueueFiltersFromSearchParams(url);
    expect(filters).toEqual(original);
    expect(warnings).toEqual([]);
  });

  it("omits page=1 from the URL (the default, not worth cluttering the address bar)", () => {
    const url = filtersToUrlSearchParams(DEFAULT_QUEUE_FILTERS);
    expect(url.has("page")).toBe(false);
  });
});

describe("parseQueueFiltersFromSearchParams: malformed input (task §7)", () => {
  it("drops a non-numeric page and warns, defaulting to page 1", () => {
    const { filters, warnings } = parseQueueFiltersFromSearchParams(new URLSearchParams("page=not-a-number"));
    expect(filters.page).toBe(1);
    expect(warnings).toHaveLength(1);
  });

  it("drops a negative or zero page", () => {
    expect(parseQueueFiltersFromSearchParams(new URLSearchParams("page=0")).filters.page).toBe(1);
    expect(parseQueueFiltersFromSearchParams(new URLSearchParams("page=-5")).filters.page).toBe(1);
  });

  it("drops an unsupported status value and warns", () => {
    const { filters, warnings } = parseQueueFiltersFromSearchParams(new URLSearchParams("status=ESCALATED"));
    expect(filters.status).toBeUndefined();
    expect(warnings[0]).toMatch(/unsupported status/i);
  });

  it("drops an unsupported priority value and warns", () => {
    const { filters, warnings } = parseQueueFiltersFromSearchParams(new URLSearchParams("priority=URGENT"));
    expect(filters.priority).toBeUndefined();
    expect(warnings[0]).toMatch(/unsupported priority/i);
  });

  it("drops an invalid batch_id (not a UUID) and warns -- task §7: invalid batch UUID", () => {
    const { filters, warnings } = parseQueueFiltersFromSearchParams(new URLSearchParams("batch_id=not-a-uuid"));
    expect(filters.batchId).toBeUndefined();
    expect(warnings[0]).toMatch(/invalid batch_id/i);
  });

  it("accepts a valid batch_id", () => {
    const { filters, warnings } = parseQueueFiltersFromSearchParams(
      new URLSearchParams("batch_id=33333333-3333-3333-3333-333333333333"),
    );
    expect(filters.batchId).toBe("33333333-3333-3333-3333-333333333333");
    expect(warnings).toEqual([]);
  });

  it("ignores an empty assigned_to rather than sending a blank filter", () => {
    const { filters } = parseQueueFiltersFromSearchParams(new URLSearchParams("assigned_to="));
    expect(filters.assignedTo).toBeUndefined();
  });
});

describe("hasActiveFilters / clearedFilters", () => {
  it("reports no active filters for the default state", () => {
    expect(hasActiveFilters(DEFAULT_QUEUE_FILTERS)).toBe(false);
  });

  it("reports active filters when any filter is set", () => {
    expect(hasActiveFilters({ page: 1, status: "OPEN" })).toBe(true);
    expect(hasActiveFilters({ page: 1, batchId: "44444444-4444-4444-4444-444444444444" })).toBe(true);
  });

  it("clears filters but resets to page 1 and preserves page size", () => {
    const cleared = clearedFilters({ page: 5, pageSize: 10, status: "OPEN", assignedTo: "x" });
    expect(cleared).toEqual({ page: 1, pageSize: 10 });
  });
});

describe("SUPPORTED_QUEUE_PRIORITY_FILTERS", () => {
  it("matches the backend's ReviewPriority enum exactly", () => {
    expect(SUPPORTED_QUEUE_PRIORITY_FILTERS).toEqual(["CRITICAL", "HIGH", "NORMAL", "LOW"]);
  });
});

describe("parseSafeReturnTo (M11B task §2/§8: breadcrumb return-to link)", () => {
  it("accepts a well-formed /review-queue path with filters", () => {
    expect(parseSafeReturnTo("/review-queue?status=OPEN&page=2")).toBe("/review-queue?status=OPEN&page=2");
  });

  it("accepts the bare /review-queue path", () => {
    expect(parseSafeReturnTo("/review-queue")).toBe("/review-queue");
  });

  it("falls back to /review-queue for undefined or an empty string", () => {
    expect(parseSafeReturnTo(undefined)).toBe("/review-queue");
    expect(parseSafeReturnTo("")).toBe("/review-queue");
  });

  it("falls back to /review-queue for an array value (a repeated ?from= param)", () => {
    expect(parseSafeReturnTo(["/review-queue?status=OPEN", "/review-queue?status=REJECTED"])).toBe("/review-queue");
  });

  it("rejects a path outside /review-queue rather than following it", () => {
    expect(parseSafeReturnTo("/dashboard")).toBe("/review-queue");
    expect(parseSafeReturnTo("/review-cases/11111111-1111-1111-1111-111111111111")).toBe("/review-queue");
  });

  it("rejects a protocol-relative or absolute URL (open-redirect prevention)", () => {
    expect(parseSafeReturnTo("//evil.example.com/review-queue")).toBe("/review-queue");
    expect(parseSafeReturnTo("https://evil.example.com/review-queue")).toBe("/review-queue");
  });

  it("rejects a value containing a backslash", () => {
    expect(parseSafeReturnTo("/review-queue\\@evil.example.com")).toBe("/review-queue");
  });
});
