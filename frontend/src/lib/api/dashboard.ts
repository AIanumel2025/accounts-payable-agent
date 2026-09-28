import "server-only";
import { callBackend } from "@/lib/server/backend-request";
import type { AppResult } from "@/lib/api/app-result";
import { withLoadedConfig } from "@/lib/api/app-result";
import type { DashboardPayload, HealthPayload } from "@/types/api-payloads";

export type { AppErrorKind, AppFailure, AppResult, AppSuccess } from "@/lib/api/app-result";

/** `GET /api/v1/dashboard` (M11A task §9). Read-only; never mutates anything. */
export function getDashboard(): Promise<AppResult<DashboardPayload>> {
  return withLoadedConfig(async (config) => {
    const result = await callBackend<DashboardPayload>("/api/v1/dashboard", config);
    if (!result.ok) return { ok: false, kind: result.kind, message: result.message };
    return { ok: true, data: result.data };
  });
}

/** `GET /health` -- backing the backend-health indicator (M11A task §8/§9). */
export function getBackendHealth(): Promise<AppResult<HealthPayload>> {
  return withLoadedConfig(async (config) => {
    const result = await callBackend<HealthPayload>("/health", config);
    if (!result.ok) return { ok: false, kind: result.kind, message: result.message };
    return { ok: true, data: result.data };
  });
}
