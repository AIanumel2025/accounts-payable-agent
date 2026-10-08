import "server-only";
import { UPSTREAM_CLERK_AUTHORIZATION_HEADER } from "@/lib/auth/reserved-headers";
import { credentialsFromEnvironment, signRequest } from "../../../lambda/aws-sigv4.mjs";

/**
 * The single outbound transport from the Next.js server to FastAPI (M11E.1).
 *
 *   - `bearer` (default; Render): the request is sent unchanged -- the Clerk session token travels as
 *     `Authorization: Bearer`. Any `x-ap-agent-clerk-authorization` header is dropped.
 *   - `aws_sigv4` (AWS): the FastAPI Function URL uses `AWS_IAM`, so SigV4 occupies `Authorization`.
 *     The Clerk token (built by `getBackendAuthHeaders` from the authenticated Clerk session -- never
 *     from the browser request) is moved into `x-ap-agent-clerk-authorization`, any pre-existing copy
 *     of that header is discarded first, and the whole request is signed with the Lambda execution
 *     role's credentials (service `lambda`). FastAPI still verifies the Clerk token independently.
 *
 * Only string/binary bodies can be signed; the staged-upload design keeps every request to FastAPI
 * small and JSON, so multipart is not used in this mode.
 */

export type PlatformAuthMode = "bearer" | "aws_sigv4";

export function loadPlatformAuthMode(env: Record<string, string | undefined> = process.env): PlatformAuthMode {
  const raw = (env.AP_AGENT_FRONTEND_PLATFORM_AUTH_MODE ?? "").trim().toLowerCase();
  if (raw === "" || raw === "bearer") return "bearer";
  if (raw === "aws_sigv4") return "aws_sigv4";
  throw new Error('AP_AGENT_FRONTEND_PLATFORM_AUTH_MODE must be "bearer" or "aws_sigv4".');
}

export interface UpstreamEnv {
  [name: string]: string | undefined;
}

type SignableBody = string | Uint8Array | undefined;

/** Pure: the headers actually sent upstream, before SigV4 signing. */
export function upstreamHeaders(source: HeadersInit | undefined, mode: PlatformAuthMode): Headers {
  const headers = new Headers(source);
  const clerk = headers.get("authorization");
  headers.delete(UPSTREAM_CLERK_AUTHORIZATION_HEADER); // never trust a pre-existing copy

  if (mode === "aws_sigv4") {
    headers.delete("authorization");
    if (clerk !== null) headers.set(UPSTREAM_CLERK_AUTHORIZATION_HEADER, clerk);
  }
  return headers;
}

export async function upstreamFetch(
  input: string | URL,
  init: RequestInit = {},
  env: UpstreamEnv = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  const mode = loadPlatformAuthMode(env);

  if (mode === "bearer") {
    // Unchanged behaviour (Render): identical `init`, except that the dedicated header can never be sent.
    const carriesReservedHeader = init.headers !== undefined && new Headers(init.headers).has(UPSTREAM_CLERK_AUTHORIZATION_HEADER);
    return fetchImpl(input, carriesReservedHeader ? { ...init, headers: upstreamHeaders(init.headers, mode) } : init);
  }

  const headers = upstreamHeaders(init.headers, mode);

  const body = init.body as SignableBody | null | undefined;
  if (body !== undefined && body !== null && typeof body !== "string" && !(body instanceof Uint8Array)) {
    throw new Error("Only string or binary bodies can be sent to the IAM-protected API.");
  }

  const region = env.AWS_REGION;
  const credentials = credentialsFromEnvironment(env);
  if (!region || !credentials) throw new Error("AWS credentials are not available to sign the request.");

  const signed = signRequest({
    method: init.method ?? "GET",
    url: String(input),
    headers: Object.fromEntries(headers.entries()),
    body: body ?? "",
    region,
    service: "lambda",
    credentials,
  });
  delete signed.host; // set by fetch itself; it is part of the signature
  return fetchImpl(input, { ...init, headers: signed });
}
