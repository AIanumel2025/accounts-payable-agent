import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const VALID_ENV = {
  AP_AGENT_API_BASE_URL: "http://127.0.0.1:8000",
  AP_AGENT_FRONTEND_AUTH_MODE: "development_headers",
  AP_AGENT_DEV_TENANT_ID: "00000000-0000-0000-0000-000000000000",
  AP_AGENT_DEV_ACTOR_ID: "local-reviewer",
  AP_AGENT_DEV_ACTOR_ROLE: "READ_ONLY_AUDITOR",
};

describe("getReviewCaseDetail: UUID validation (M11B task §8)", () => {
  beforeEach(() => {
    vi.stubEnv("AP_AGENT_API_BASE_URL", VALID_ENV.AP_AGENT_API_BASE_URL);
    vi.stubEnv("AP_AGENT_FRONTEND_AUTH_MODE", VALID_ENV.AP_AGENT_FRONTEND_AUTH_MODE);
    vi.stubEnv("AP_AGENT_DEV_TENANT_ID", VALID_ENV.AP_AGENT_DEV_TENANT_ID);
    vi.stubEnv("AP_AGENT_DEV_ACTOR_ID", VALID_ENV.AP_AGENT_DEV_ACTOR_ID);
    vi.stubEnv("AP_AGENT_DEV_ACTOR_ROLE", VALID_ENV.AP_AGENT_DEV_ACTOR_ROLE);
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("never calls the backend for a malformed id -- returns NOT_FOUND locally", async () => {
    const { getReviewCaseDetail } = await import("@/lib/api/review-cases");
    const result = await getReviewCaseDetail("not-a-uuid");

    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.kind).toBe("NOT_FOUND");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("never calls the backend for an empty id", async () => {
    const { getReviewCaseDetail } = await import("@/lib/api/review-cases");
    const result = await getReviewCaseDetail("");

    expect(result.ok).toBe(false);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("calls the backend for a syntactically valid UUID", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({ request_id: "r1", status: "SUCCEEDED", data: {}, errors: [], generated_at: "2026-01-01T00:00:00Z" }),
        { status: 200 },
      ),
    );
    const { getReviewCaseDetail } = await import("@/lib/api/review-cases");
    await getReviewCaseDetail("11111111-1111-1111-1111-111111111111");

    expect(fetch).toHaveBeenCalledTimes(1);
    const [url] = vi.mocked(fetch).mock.calls[0]!;
    expect(String(url)).toContain("/api/v1/review-cases/11111111-1111-1111-1111-111111111111");
  });
});

describe("listReviewCases (M11B task §7)", () => {
  beforeEach(() => {
    vi.stubEnv("AP_AGENT_API_BASE_URL", VALID_ENV.AP_AGENT_API_BASE_URL);
    vi.stubEnv("AP_AGENT_FRONTEND_AUTH_MODE", VALID_ENV.AP_AGENT_FRONTEND_AUTH_MODE);
    vi.stubEnv("AP_AGENT_DEV_TENANT_ID", VALID_ENV.AP_AGENT_DEV_TENANT_ID);
    vi.stubEnv("AP_AGENT_DEV_ACTOR_ID", VALID_ENV.AP_AGENT_DEV_ACTOR_ID);
    vi.stubEnv("AP_AGENT_DEV_ACTOR_ROLE", VALID_ENV.AP_AGENT_DEV_ACTOR_ROLE);
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("sends the exact FastAPI query-parameter names for every set filter", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          request_id: "r1",
          status: "SUCCEEDED",
          data: { items: [], pagination: { page: 1, page_size: 25, total_count: 0, total_pages: 1 } },
          errors: [],
          generated_at: "2026-01-01T00:00:00Z",
        }),
        { status: 200 },
      ),
    );
    const { listReviewCases } = await import("@/lib/api/review-cases");
    await listReviewCases({ page: 2, status: "OPEN", priority: "HIGH", assignedTo: "reviewer-1" });

    expect(fetch).toHaveBeenCalledTimes(1);
    const [url] = vi.mocked(fetch).mock.calls[0]!;
    const requested = new URL(String(url));
    expect(requested.pathname).toContain("/api/v1/review-cases");
    expect(requested.searchParams.get("page")).toBe("2");
    expect(requested.searchParams.get("status")).toBe("OPEN");
    expect(requested.searchParams.get("priority")).toBe("HIGH");
    expect(requested.searchParams.get("assigned_to")).toBe("reviewer-1");
  });

  it("surfaces a non-2xx backend response as a failed AppResult, not a thrown error", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ errors: ["INTERNAL_ERROR"] }), { status: 500 }));
    const { listReviewCases } = await import("@/lib/api/review-cases");
    const result = await listReviewCases({ page: 1 });

    expect(result.ok).toBe(false);
  });
});
