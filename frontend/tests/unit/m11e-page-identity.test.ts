// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  redirect: vi.fn((target: string) => {
    throw new Error(`NEXT_REDIRECT:${target}`);
  }),
  getBackendAuthHeaders: vi.fn(),
  callBackend: vi.fn(),
  loadServerEnvConfig: vi.fn(),
}));

vi.mock("next/navigation", () => ({ redirect: mocks.redirect }));
vi.mock("@/lib/auth/backend-auth", () => ({ getBackendAuthHeaders: mocks.getBackendAuthHeaders }));
vi.mock("@/lib/server/backend-request", () => ({ callBackend: mocks.callBackend }));
vi.mock("@/lib/config/server-env", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/config/server-env")>()),
  loadServerEnvConfig: mocks.loadServerEnvConfig,
}));

import { requirePageIdentity } from "@/lib/auth/identity";

const CLERK = { apiBaseUrl: "http://private-api:8000", authMode: "clerk_jwt" };

describe("requirePageIdentity", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.loadServerEnvConfig.mockReturnValue(CLERK);
    mocks.getBackendAuthHeaders.mockResolvedValue({ ok: true, headers: { Authorization: "Bearer t" } });
  });

  it("uses the development role in development mode and never calls the backend session endpoint", async () => {
    mocks.loadServerEnvConfig.mockReturnValue({ apiBaseUrl: "x", authMode: "development_headers", devActorRole: "AP_REVIEWER" });
    expect(await requirePageIdentity()).toEqual({ role: "AP_REVIEWER", tenantName: null, hosted: false, configErrorMessage: null });
    expect(mocks.callBackend).not.toHaveBeenCalled();
  });

  it("takes the role and organization name from the backend's database mapping", async () => {
    mocks.callBackend.mockResolvedValue({ ok: true, status: 200, data: { role: "AP_OPERATOR", tenant_display_name: "Example Ltd", auth_mode: "clerk_jwt" } });
    expect(await requirePageIdentity()).toEqual({ role: "AP_OPERATOR", tenantName: "Example Ltd", hosted: true, configErrorMessage: null });
    expect(mocks.callBackend).toHaveBeenCalledWith("/api/v1/session", CLERK);
  });

  it("sends a signed-out visitor to sign-in and a user without an organization to organization selection", async () => {
    mocks.getBackendAuthHeaders.mockResolvedValueOnce({ ok: false, kind: "SIGN_IN_REQUIRED" });
    await expect(requirePageIdentity()).rejects.toThrow("NEXT_REDIRECT:/sign-in");
    mocks.getBackendAuthHeaders.mockResolvedValueOnce({ ok: false, kind: "ORGANIZATION_REQUIRED" });
    await expect(requirePageIdentity()).rejects.toThrow("NEXT_REDIRECT:/organization-required");
    expect(mocks.callBackend).not.toHaveBeenCalled();
  });

  it.each([
    [["IDENTITY_NOT_MAPPED"], 403, "not-mapped"],
    [["IDENTITY_MEMBERSHIP_INACTIVE"], 403, "inactive"],
    [["TOKEN_EXPIRED"], 401, "session-expired"],
    [["TOKEN_SIGNATURE_INVALID"], 401, "signed-out"],
    [["AUTHENTICATION_UNAVAILABLE"], 503, "unavailable"],
  ])("renders the account-state page for backend codes %j", async (errorCodes, status, reason) => {
    mocks.callBackend.mockResolvedValue({ ok: false, kind: "FORBIDDEN", status, message: "x", errorCodes });
    await expect(requirePageIdentity()).rejects.toThrow(`NEXT_REDIRECT:/access-denied?reason=${reason}`);
  });

  it("falls through (the page shows its own error) when the backend is merely unreachable", async () => {
    mocks.callBackend.mockResolvedValue({ ok: false, kind: "UNAVAILABLE", message: "x" });
    expect(await requirePageIdentity()).toEqual({ role: "UNKNOWN", tenantName: null, hosted: true, configErrorMessage: null });
  });
});
