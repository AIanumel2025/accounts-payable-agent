import "server-only";
import { buildDevelopmentAuthHeaders } from "@/lib/auth/dev-headers";
import type { ServerEnvConfig } from "@/lib/config/server-env";

/**
 * The single place the Next.js server decides what identity it presents to
 * the private FastAPI service (M11E).
 *
 *   - `development_headers` (local development / automated tests only): the
 *     four prototype headers, built from server-only configuration.
 *   - `clerk_jwt` (hosted): `Authorization: Bearer <Clerk session token>`,
 *     obtained from the signed-in session on the server. Nothing identity-
 *     shaped is ever taken from the browser request; FastAPI independently
 *     verifies the token and resolves tenant and role from its own database.
 *
 * Failures are reduced to three kinds so callers can answer with a fixed,
 * safe state; the token and session details never leave this module.
 */

export type AuthFailureKind = "SIGN_IN_REQUIRED" | "ORGANIZATION_REQUIRED" | "AUTH_UNAVAILABLE";

export type BackendAuthResult =
  | { ok: true; headers: Record<string, string> }
  | { ok: false; kind: AuthFailureKind };

export interface SessionSnapshot {
  userId: string | null;
  orgId: string | null | undefined;
  token: string | null;
}

export type SessionSource = () => Promise<SessionSnapshot>;

export async function clerkSessionSource(): Promise<SessionSnapshot> {
  const { auth } = await import("@clerk/nextjs/server");
  const session = await auth();
  const token = session.userId ? await session.getToken() : null;
  return { userId: session.userId, orgId: session.orgId, token };
}

export async function getBackendAuthHeaders(
  config: ServerEnvConfig,
  now: Date = new Date(),
  source: SessionSource = clerkSessionSource,
): Promise<BackendAuthResult> {
  if (config.authMode === "development_headers") {
    return { ok: true, headers: buildDevelopmentAuthHeaders(config, now) };
  }

  let snapshot: SessionSnapshot;
  try {
    snapshot = await source();
  } catch {
    return { ok: false, kind: "AUTH_UNAVAILABLE" };
  }

  if (!snapshot.userId || !snapshot.token) return { ok: false, kind: "SIGN_IN_REQUIRED" };
  if (!snapshot.orgId) return { ok: false, kind: "ORGANIZATION_REQUIRED" };

  return { ok: true, headers: { Authorization: `Bearer ${snapshot.token}` } };
}

/** Stable, user-safe error codes for a failed authentication step (JSON routes). */
export function authFailureCode(kind: AuthFailureKind): { status: number; code: string } {
  switch (kind) {
    case "SIGN_IN_REQUIRED":
      return { status: 401, code: "AUTHENTICATION_REQUIRED" };
    case "ORGANIZATION_REQUIRED":
      return { status: 403, code: "ORGANIZATION_REQUIRED" };
    case "AUTH_UNAVAILABLE":
      return { status: 503, code: "AUTHENTICATION_UNAVAILABLE" };
  }
}
