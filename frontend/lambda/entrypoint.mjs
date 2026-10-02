// Lambda container entry point (M11E.1): load secrets from SSM, then start the Next.js standalone server
// that the Lambda Web Adapter proxies to. See `bootstrap.mjs`.

import { loadSsmParameters } from "./bootstrap.mjs";

try {
  const loaded = await loadSsmParameters();
  console.log(`bootstrap: loaded ${loaded.length} secret(s) from the secret store.`);
} catch (error) {
  console.error(`bootstrap: ${error instanceof Error ? error.message : "secret loading failed"}`);
  process.exit(1);
}

await import("./server.js");
