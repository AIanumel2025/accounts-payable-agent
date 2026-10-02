import "server-only";
import { redirect } from "next/navigation";
import { authReasonFromCodes, type AuthReason } from "@/lib/auth/auth-reasons";
import { getBackendAuthHeaders } from "@/lib/auth/backend-auth";
import { ServerConfigError, loadServerEnvConfig } from "@/lib/config/server-env";
import { callBackend } from "@/lib/server/backend-request";

/**
 * What every protected page needs to know about the caller (M11E).
 *
 *   - development headers: the configured development role;
 *   - hosted `clerk_jwt`: the role and tenant name FastAPI resolves from its
 *     own database mapping (`GET /api/v1/session`). A browser-supplied or
 *     token-supplied role is never used.
 *
 * In hosted mode an authentication or account problem redirects to the
 * account-state page (`/access-denied?reason=...`) or the sign-in /
 * organization pages, so the page body never renders without an identity.
 * Other failures (backend unreachable) fall through with an `UNKNOWN` role
 * and the page shows its own error state.
 */

export interface PageIdentity {
  role: string;
  tenantName: string | null;
  /** True when identity comes from the hosted (Clerk + database mapping) path. */
  hosted: boolean;
  configErrorMessage: string | null;
}

interface SessionPayload {
  role: string;
  tenant_display_name: string | null;
}

function toAccountState(reason: AuthReason): never {
  redirect(`/access-denied?reason=${reason}`);
}

export async function requirePageIdentity(): Promise<PageIdentity> {
  let config;
  try {
    config = loadServerEnvConfig();
  } catch (error) {
    if (error instanceof ServerConfigError) {
      return { role: "UNKNOWN", tenantName: null, hosted: false, configErrorMessage: error.message };
    }
    throw error;
  }

  if (config.authMode === "development_headers") {
    return { role: config.devActorRole ?? "UNKNOWN", tenantName: null, hosted: false, configErrorMessage: null };
  }

  const auth = await getBackendAuthHeaders(config);
  if (!auth.ok) {
    if (auth.kind === "SIGN_IN_REQUIRED") redirect("/sign-in");
    if (auth.kind === "ORGANIZATION_REQUIRED") redirect("/organization-required");
    return toAccountState("unavailable");
  }

  const result = await callBackend<SessionPayload>("/api/v1/session", config);
  if (result.ok) {
    return { role: result.data.role, tenantName: result.data.tenant_display_name, hosted: true, configErrorMessage: null };
  }

  const reason = authReasonFromCodes(result.errorCodes ?? [], result.status);
  if (reason !== null) return toAccountState(reason);

  return { role: "UNKNOWN", tenantName: null, hosted: true, configErrorMessage: null };
}
