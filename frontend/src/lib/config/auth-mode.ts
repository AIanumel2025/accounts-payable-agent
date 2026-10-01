/**
 * Whether this deployment authenticates users through Clerk (hosted) rather
 * than the development header adapter. Read from the server environment on
 * every call; safe to import from Server Components, the proxy and route
 * handlers. Not exported to the browser: the value is only ever used to
 * choose what the server renders.
 */
export function isClerkAuthMode(source: Record<string, string | undefined> = process.env): boolean {
  return source.AP_AGENT_FRONTEND_AUTH_MODE === "clerk_jwt";
}
