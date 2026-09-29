import "server-only";
import { loadServerEnvConfig, ServerConfigError } from "@/lib/config/server-env";
import type { BackendErrorKind } from "@/lib/server/backend-request";

/** Shared server-side result type for every `lib/api/*` fetcher (M11A task §10, reused by M11B's queue/detail fetchers). */

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

export function withLoadedConfig<T>(
  run: (config: ReturnType<typeof loadServerEnvConfig>) => Promise<AppResult<T>>,
): Promise<AppResult<T>> {
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
