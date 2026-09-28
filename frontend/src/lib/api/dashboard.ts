import "server-only";
import { loadServerEnvConfig, ServerConfigError } from "@/lib/config/server-env";
import { callBackend, type BackendErrorKind } from "@/lib/server/backend-request";
import type { DashboardPayload, HealthPayload } from "@/types/api-payloads";

export type AppErrorKind = BackendErrorKind | "CONFIG_ERROR";

export interface AppSuccess<T> {
  ok: true;
  data: T;
}

export interface AppFailure {
  ok: false;
  kind: AppErrorKind;
  message: string;
}

export type AppResult<T> = AppSuccess<T> | AppFailure;

function withLoadedConfig<T>(run: (config: ReturnType<typeof loadServerEnvConfig>) => Promise<AppResult<T>>): Promise<AppResult<T>> {
  let config: ReturnType<typeof loadServerEnvConfig>;
  try {
    config = loadServerEnvConfig();
  } catch (error) {
    if (error instanceof ServerConfigError) {
      return Promise.resolve({ ok: false, kind: "CONFIG_ERROR", message: error.message });
    }
    throw error;
  }
  return run(config);
}

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
