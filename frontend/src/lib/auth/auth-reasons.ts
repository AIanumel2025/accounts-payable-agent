/**
 * Pure mapping from backend authentication/authorization error codes to the
 * small, fixed set of user-facing account states (M11E). No secrets, no
 * identifiers: only stable codes in, a whitelisted reason out. Safe to import
 * from client components.
 */

export const AUTH_REASONS = [
  "signed-out",
  "session-expired",
  "no-organization",
  "not-mapped",
  "inactive",
  "unavailable",
] as const;

export type AuthReason = (typeof AUTH_REASONS)[number];

export function isAuthReason(value: string | null | undefined): value is AuthReason {
  return typeof value === "string" && (AUTH_REASONS as readonly string[]).includes(value);
}

/** Maps the stable error codes FastAPI returns to an account state, or `null` when the codes are not authentication-related. */
export function authReasonFromCodes(codes: readonly string[], status?: number): AuthReason | null {
  const has = (code: string) => codes.includes(code);

  if (has("TOKEN_EXPIRED")) return "session-expired";
  if (has("ORGANIZATION_REQUIRED")) return "no-organization";
  if (has("IDENTITY_NOT_MAPPED") || has("TENANT_ACCESS_DENIED")) return "not-mapped";
  if (has("IDENTITY_MEMBERSHIP_INACTIVE")) return "inactive";
  if (has("AUTHENTICATION_UNAVAILABLE")) return "unavailable";
  if (codes.some((code) => code.startsWith("TOKEN_"))) return "signed-out";
  // A bare 401 (no recognised code) still means the caller is not signed in.
  if (status === 401) return "signed-out";
  return null;
}

export interface AuthReasonPresentation {
  title: string;
  message: string;
  /** Which recovery actions the account-state page offers. */
  actions: ReadonlyArray<"sign-in" | "sign-out" | "switch-organization" | "retry">;
}

export const AUTH_REASON_PRESENTATION: Record<AuthReason, AuthReasonPresentation> = {
  "signed-out": {
    title: "Sign in required",
    message: "Your sign-in could not be verified. Please sign in again to continue.",
    actions: ["sign-out"],
  },
  "session-expired": {
    title: "Your session has expired",
    message: "For your security you were signed out after a period of inactivity. Please sign in again.",
    actions: ["sign-out"],
  },
  "no-organization": {
    title: "Choose an organization",
    message: "Select the organization you want to work in. Invoices and reviews are always scoped to one organization.",
    actions: ["switch-organization", "sign-out"],
  },
  "not-mapped": {
    title: "This account is not set up yet",
    message:
      "You are signed in, but this account has not been registered for this organization. Ask your administrator to grant access, then try again.",
    actions: ["switch-organization", "retry", "sign-out"],
  },
  inactive: {
    title: "Your access is inactive",
    message: "Your membership for this organization has been disabled. Contact your administrator if you believe this is a mistake.",
    actions: ["switch-organization", "sign-out"],
  },
  unavailable: {
    title: "Sign-in could not be verified right now",
    message: "The service could not verify your sign-in at the moment. Nothing was changed. Please try again shortly.",
    actions: ["retry", "sign-out"],
  },
};
