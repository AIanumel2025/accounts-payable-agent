import { NextRequest, NextResponse } from "next/server";
import { buildDevelopmentAuthHeaders } from "@/lib/auth/dev-headers";
import { loadServerEnvConfig, ServerConfigError } from "@/lib/config/server-env";

/**
 * Server-side API boundary the browser talks to (M11A task §5): the only
 * FastAPI-shaped endpoint reachable from client JavaScript. Used by
 * client-side "retry" affordances (task §10) so the retry doesn't need a
 * full page reload; the initial dashboard render instead calls
 * `src/lib/api/dashboard.ts` directly from a Server Component, which never
 * round-trips through this HTTP route.
 *
 * Read-only safety (task §6), enforced here, not just by omission in UI
 * code:
 *   - Only a `GET` handler is exported, so Next.js itself returns 405 for
 *     any other method at this route -- there is no code path that could
 *     forward a POST to `/api/v1/review-cases/{id}/commands`.
 *   - `ALLOWED_BACKEND_PATHS` is an explicit allow-list, not a bare
 *     pass-through: any path outside it (including every review-command
 *     endpoint) is rejected with 404 before a request is ever built.
 *   - The Next.js server attaches only its own development-header
 *     authentication (task §5) -- it never forwards a browser-supplied
 *     `Authorization`/`X-Tenant-ID`/etc. header, so a client cannot spoof
 *     tenant identity through this route.
 */

const ALLOWED_BACKEND_PATHS = new Set<string>(["health", "api/v1/dashboard"]);

const REQUEST_TIMEOUT_MS = Number(process.env.AP_AGENT_BACKEND_TIMEOUT_MS ?? 8_000);

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const joinedPath = path.join("/");

  if (!ALLOWED_BACKEND_PATHS.has(joinedPath)) {
    return NextResponse.json(
      { errors: ["BACKEND_PATH_NOT_ALLOWED"], generated_at: new Date().toISOString() },
      { status: 404 },
    );
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

  const upstreamUrl = new URL(`${config.apiBaseUrl}/${joinedPath}`);
  const incomingSearchParams = request.nextUrl.searchParams;
  for (const [key, value] of incomingSearchParams.entries()) {
    upstreamUrl.searchParams.set(key, value);
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
