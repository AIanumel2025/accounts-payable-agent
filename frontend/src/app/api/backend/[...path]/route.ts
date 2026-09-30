import { NextRequest, NextResponse } from "next/server";
import { buildDevelopmentAuthHeaders } from "@/lib/auth/dev-headers";
import { loadServerEnvConfig, ServerConfigError } from "@/lib/config/server-env";
import { loadOperationsMode } from "@/lib/config/operations-mode";

/**
 * Server-side API boundary the browser talks to (M11A task §5, extended
 * M11B task §5 for the review queue/detail endpoints): the only
 * FastAPI-shaped endpoint reachable from client JavaScript. Used by
 * client-side "retry" affordances (task §10) so the retry doesn't need a
 * full page reload; the initial dashboard/queue/detail render instead
 * calls `src/lib/api/*.ts` directly from a Server Component, which never
 * round-trips through this HTTP route.
 *
 * Read-only safety (task §6), enforced here, not just by omission in UI
 * code:
 *   - Only a `GET` handler is exported, so Next.js itself returns 405 for
 *     any other method at this route -- there is no code path that could
 *     forward a POST to `/api/v1/review-cases/{id}/commands`.
 *   - `matchAllowedPath` is an explicit, shape-checked allow-list, not a
 *     bare pass-through: `/api/v1/review-cases/{uuid}` is only matched
 *     when the path has *exactly* four segments and the fourth is a
 *     syntactically valid UUID -- a fifth segment (`/commands`, or
 *     anything else) never matches, so no review-command endpoint is
 *     reachable through this route under any input, and a malformed or
 *     non-UUID id is rejected with 404 before a request is ever built
 *     (task §5: "validate review-case UUIDs").
 *   - Query parameters are filtered to each matched path's own
 *     FastAPI-supported allow-list (task §5: "allow only query parameters
 *     supported by FastAPI") -- an unsupported parameter is silently
 *     dropped, never forwarded.
 *   - The Next.js server attaches only its own development-header
 *     authentication (task §5) -- it never forwards a browser-supplied
 *     `Authorization`/`X-Tenant-ID`/etc. header, so a client cannot spoof
 *     tenant identity through this route, and tenant identity is never
 *     read from a route/query parameter here.
 */

const UUID_PATTERN = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

const REQUEST_TIMEOUT_MS = Number(process.env.AP_AGENT_BACKEND_TIMEOUT_MS ?? 8_000);

interface AllowedPathMatch {
  allowedQueryParams: ReadonlySet<string>;
  /** M11D Core: operations read paths are only served while the operations mode is enabled. */
  requiresOperations?: boolean;
}

/**
 * The complete allow-list. Every path this proxy will ever forward is
 * enumerated here by exact shape -- nothing is inferred from the incoming
 * request beyond matching one of these. Segments come from Next.js's
 * catch-all `[...path]` array, each already URL-decoded by Next itself;
 * an encoded traversal attempt (`%2e%2e`, `..`) decodes to a literal `..`
 * segment, which matches none of these shapes and is rejected below like
 * any other unrecognized path -- no separate traversal-specific check is
 * needed because nothing here does string concatenation into a file path
 * or regex that `..` could escape.
 */
export function matchAllowedPath(segments: readonly string[]): AllowedPathMatch | null {
  if (segments.length === 0 || segments.some((segment) => segment.length === 0)) {
    return null;
  }

  const joined = segments.join("/");

  if (joined === "health") {
    return { allowedQueryParams: new Set() };
  }

  if (joined === "api/v1/dashboard") {
    return { allowedQueryParams: new Set(["batch_id"]) };
  }

  if (joined === "api/v1/review-cases") {
    return {
      allowedQueryParams: new Set(["page", "page_size", "status", "assigned_to", "priority", "batch_id"]),
    };
  }

  if (
    segments.length === 4 &&
    segments[0] === "api" &&
    segments[1] === "v1" &&
    segments[2] === "review-cases" &&
    UUID_PATTERN.test(segments[3]!)
  ) {
    return { allowedQueryParams: new Set() };
  }

  // M11D Core: read-only job list and job detail (exact shapes; no submissions, no sub-paths).
  if (joined === "api/v1/operations/jobs") {
    return {
      allowedQueryParams: new Set(["page", "page_size", "status", "job_type", "review_case_id"]),
      requiresOperations: true,
    };
  }

  if (
    segments.length === 5 &&
    segments[0] === "api" &&
    segments[1] === "v1" &&
    segments[2] === "operations" &&
    segments[3] === "jobs" &&
    UUID_PATTERN.test(segments[4]!)
  ) {
    return { allowedQueryParams: new Set(), requiresOperations: true };
  }

  return null;
}

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const match = matchAllowedPath(path);

  if (!match) {
    return NextResponse.json(
      { errors: ["BACKEND_PATH_NOT_ALLOWED"], generated_at: new Date().toISOString() },
      { status: 404 },
    );
  }

  if (match.requiresOperations) {
    const operationsMode = loadOperationsMode();
    if (!operationsMode.valid || operationsMode.mode !== "enabled") {
      return NextResponse.json(
        { errors: ["OPERATIONS_DISABLED"], generated_at: new Date().toISOString() },
        { status: 404 },
      );
    }
  }

  let config;
  try {
    config = loadServerEnvConfig();
  } catch (error) {
    if (error instanceof ServerConfigError) {
      return NextResponse.json(
        { errors: ["FRONTEND_AUTH_MISCONFIGURED"], generated_at: new Date().toISOString() },
        { status: 500 },
      );
    }
    throw error;
  }

  const joinedPath = path.join("/");
  const upstreamUrl = new URL(`${config.apiBaseUrl}/${joinedPath}`);
  const incomingSearchParams = request.nextUrl.searchParams;
  for (const [key, value] of incomingSearchParams.entries()) {
    if (match.allowedQueryParams.has(key)) {
      upstreamUrl.searchParams.set(key, value);
    }
  }

  const headers = buildDevelopmentAuthHeaders(config);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const upstreamResponse = await fetch(upstreamUrl, {
      method: "GET",
      headers,
      signal: controller.signal,
      cache: "no-store",
    });
    const body = await upstreamResponse.text();
    return new NextResponse(body, {
      status: upstreamResponse.status,
      headers: { "content-type": upstreamResponse.headers.get("content-type") ?? "application/json" },
    });
  } catch (error) {
    const kind = error instanceof DOMException && error.name === "AbortError" ? "BACKEND_TIMEOUT" : "BACKEND_UNAVAILABLE";
    return NextResponse.json({ errors: [kind], generated_at: new Date().toISOString() }, { status: 503 });
  } finally {
    clearTimeout(timeout);
  }
}
