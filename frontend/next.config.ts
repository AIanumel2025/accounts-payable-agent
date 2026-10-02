import type { NextConfig } from "next";

// Server-only configuration is read exclusively inside `src/lib/config` and
// `src/lib/server`, never here and never with a `NEXT_PUBLIC_` prefix, so
// nothing server-side leaks into the client bundle (M11A task §5).
const nextConfig: NextConfig = {
  reactStrictMode: true,
  // AWS Lambda image only (M11E.1): `docker/web.lambda.Dockerfile` sets this so the build emits a
  // self-contained `.next/standalone` server. Unset everywhere else (Render, tests), where `next start` is used.
  ...(process.env.AP_AGENT_NEXT_OUTPUT === "standalone" ? { output: "standalone" as const } : {}),
  poweredByHeader: false,
  // No external font downloads at runtime (task §17): the design system
  // uses a bundled system-font stack only (see src/styles/tokens.css).
  // `next build` already fails on lint/type errors by default in this
  // version (no `ignoreDuringBuilds`/`ignoreBuildErrors` escape hatch is
  // set here), so build failures surface, not get silently swallowed.
};

export default nextConfig;
