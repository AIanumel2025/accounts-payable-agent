// Vitest has no equivalent to Next.js's "react-server" bundler condition,
// so the real `server-only` package (which throws unless that condition is
// set) is aliased to this no-op for unit/component tests. The modules that
// import `server-only` are still genuinely server-only in the built
// application -- Next.js's own build enforces that boundary.
export {};
