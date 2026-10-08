/**
 * Headers that carry server-to-server identity (M11E.1) and therefore must never be accepted from a
 * browser. `x-ap-agent-clerk-authorization` is the header the Next.js server uses to present the Clerk
 * session token to the IAM-protected FastAPI Function URL (where SigV4 occupies `Authorization`). Only
 * `lib/server/upstream` ever sets it, built from the authenticated Clerk session; `src/proxy.ts` removes
 * any browser-supplied copy (and the prototype identity headers) from every inbound request before
 * anything else runs.
 */

export const UPSTREAM_CLERK_AUTHORIZATION_HEADER = "x-ap-agent-clerk-authorization";

const RESERVED_INBOUND_HEADERS = [
  UPSTREAM_CLERK_AUTHORIZATION_HEADER,
  "x-tenant-id",
  "x-actor-id",
  "x-actor-role",
  "x-authenticated-at",
  "x-amz-security-token",
  "x-amz-content-sha256",
  "x-amz-date",
] as const;

/** A copy of `headers` without any reserved server-to-server header. */
export function stripReservedInboundHeaders(headers: Headers): Headers {
  const cleaned = new Headers(headers);
  for (const name of RESERVED_INBOUND_HEADERS) cleaned.delete(name);
  return cleaned;
}

export function hasReservedInboundHeader(headers: Headers): boolean {
  return RESERVED_INBOUND_HEADERS.some((name) => headers.has(name));
}
