import "server-only";
import type { ServerEnvConfig } from "@/lib/config/server-env";

/**
 * Builds the M10 FastAPI development/test header-authentication adapter's
 * four headers (M11A task §5). This is explicitly NOT production
 * authentication -- see `docs/m11a_frontend_foundation_report.md`,
 * "Development-authentication limitation", and
 * `src/ap_agent/api/dependencies.py`'s own module docstring on the backend.
 *
 * Tenant identity is always taken from server-only configuration, never
 * from a browser-controlled query parameter, cookie, or request header
 * (task §5: "Do not accept tenant identity from browser-controlled query
 * parameters."). `X-Authenticated-At` is generated fresh per call from a
 * timezone-aware current timestamp, never cached or reused across
 * requests.
 */
export function buildDevelopmentAuthHeaders(config: ServerEnvConfig): Record<string, string> {
  return {
    "X-Tenant-ID": config.devTenantId,
    "X-Actor-ID": config.devActorId,
    "X-Actor-Role": config.devActorRole,
    "X-Authenticated-At": new Date().toISOString(),
  };
}
