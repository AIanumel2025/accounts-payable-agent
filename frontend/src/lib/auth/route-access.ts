/**
 * Server-boundary route policy for hosted mode (M11E), as a pure function so
 * every branch is unit-testable. `src/proxy.ts` applies it to every request
 * *before* any page or route handler runs -- protection never depends on
 * client-side redirects.
 *
 *   /sign-in, /sign-up        public
 *   /organization-required,   need a signed-in user, not an active organization
 *   /access-denied
 *   /api/*                    need user + active organization; answered with
 *                             JSON (never a redirect to an HTML page)
 *   everything else           need user + active organization; otherwise
 *                             redirect to sign-in / organization selection
 */

export type AccessDecision =
  | { action: "allow" }
  | { action: "redirect"; to: string }
  | { action: "json"; status: 401 | 403; code: "AUTHENTICATION_REQUIRED" | "ORGANIZATION_REQUIRED" };

export interface AccessInput {
  pathname: string;
  /** Path plus query, used only to return the user to where they were going. */
  returnTo?: string;
  isSignedIn: boolean;
  hasOrganization: boolean;
}

const PUBLIC_PREFIXES = ["/sign-in", "/sign-up"];
const ACCOUNT_STATE_PATHS = ["/organization-required", "/access-denied"];

function matchesPrefix(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

/** Only same-origin relative paths may be used as a post-sign-in destination. */
export function safeReturnPath(candidate: string | undefined): string {
  if (!candidate || !candidate.startsWith("/") || candidate.startsWith("//") || candidate.includes("\\")) return "/dashboard";
  if (PUBLIC_PREFIXES.some((prefix) => matchesPrefix(candidate.split("?")[0] ?? "", prefix))) return "/dashboard";
  return candidate;
}

export function decideAccess(input: AccessInput): AccessDecision {
  const { pathname, isSignedIn, hasOrganization } = input;

  if (PUBLIC_PREFIXES.some((prefix) => matchesPrefix(pathname, prefix))) return { action: "allow" };

  const isApi = pathname === "/api" || pathname.startsWith("/api/");

  if (!isSignedIn) {
    if (isApi) return { action: "json", status: 401, code: "AUTHENTICATION_REQUIRED" };
    const returnTo = safeReturnPath(input.returnTo ?? pathname);
    return { action: "redirect", to: `/sign-in?redirect_url=${encodeURIComponent(returnTo)}` };
  }

  if (ACCOUNT_STATE_PATHS.some((path) => matchesPrefix(pathname, path))) return { action: "allow" };

  if (!hasOrganization) {
    if (isApi) return { action: "json", status: 403, code: "ORGANIZATION_REQUIRED" };
    return { action: "redirect", to: "/organization-required" };
  }

  return { action: "allow" };
}
