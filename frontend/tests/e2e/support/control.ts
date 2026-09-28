import type { APIRequestContext } from "@playwright/test";

const MOCK_BACKEND_URL = "http://127.0.0.1:4312";

export type MockBackendMode = "normal" | "unavailable" | "malformed" | "timeout";

export async function setMockBackendMode(request: APIRequestContext, mode: MockBackendMode): Promise<void> {
  const response = await request.post(`${MOCK_BACKEND_URL}/__control`, { data: { mode } });
  if (!response.ok()) {
    throw new Error(`Failed to set mock backend mode to ${mode}: ${response.status()}`);
  }
}
