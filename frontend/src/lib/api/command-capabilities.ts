import "server-only";
import type { AppResult } from "@/lib/api/app-result";
import { withLoadedConfig } from "@/lib/api/app-result";
import { parseCapabilities } from "@/lib/commands/capabilities";
import { UUID_PATTERN } from "@/lib/commands/contract";
import { callBackend } from "@/lib/server/backend-request";
import type { CommandCapabilitiesPayload } from "@/types/api-payloads";

/**
 * `GET /api/v1/review-cases/{id}/command-capabilities` (M11C task §6).
 * Server-side only: called from the detail page's Server Component, so the
 * browser never needs a GET route for it. The result is advisory -- the
 * command executor re-validates everything transactionally.
 */
export function getCommandCapabilities(reviewCaseId: string): Promise<AppResult<CommandCapabilitiesPayload>> {
  if (!UUID_PATTERN.test(reviewCaseId)) {
    return Promise.resolve({ ok: false, kind: "NOT_FOUND", message: "This review case could not be found." });
  }

  return withLoadedConfig(async (config) => {
    const result = await callBackend<unknown>(`/api/v1/review-cases/${reviewCaseId}/command-capabilities`, config);
    if (!result.ok) return { ok: false, kind: result.kind, message: result.message };
    const capabilities = parseCapabilities(result.data);
    if (capabilities === null) {
      return { ok: false, kind: "MALFORMED_RESPONSE", message: "The backend returned unexpected command capabilities." };
    }
    return { ok: true, data: capabilities };
  });
}
