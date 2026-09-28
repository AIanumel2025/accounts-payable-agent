#!/usr/bin/env node
// Scans the production build output for server-only secrets (M11A task
// §16). Run after `npm run build`. Fails closed: any match is a failure,
// and the script lists every offending file so it's actionable in CI.
//
// This complements (does not replace) the browser-side checks in
// `tests/e2e/secret-exposure.spec.ts`, which check the live rendered page,
// browser storage, cookies, and console -- this script checks the actual
// files shipped to the browser: `.next/static` (JS bundles) and any
// generated source maps.

import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const STATIC_DIR = path.join(FRONTEND_ROOT, ".next", "static");

// Deliberately does not include the string "development_headers" or role
// *labels* (e.g. "Read-Only Auditor") -- those are intended, non-secret UI
// copy (task §8's actor-role indicator). It includes the raw
// AP_AGENT_DEV_ACTOR_ROLE *value* as configured for CI/local runs
// (task §16: "server-only role configuration").
const FORBIDDEN_PATTERNS = [
  { name: "AP_AGENT_TEST_POSTGRES_DSN literal", pattern: /AP_AGENT_TEST_POSTGRES_DSN\s*=\s*postgres/i },
  { name: "AP_AGENT_POSTGRES_DSN literal", pattern: /AP_AGENT_POSTGRES_DSN\s*=\s*postgres/i },
  { name: "PostgreSQL connection string", pattern: /postgres(?:ql)?:\/\/[^\s"'<>]+:[^\s"'<>]+@/i },
  { name: "Neon hostname", pattern: /neon\.tech/i },
  { name: "development tenant id", pattern: /00000000-0000-0000-0000-000000000000/ },
  { name: "development actor id (default)", pattern: /local-reviewer/ },
  { name: "server-only role configuration value", pattern: /READ_ONLY_AUDITOR/ },
];

function walk(dir) {
  const entries = readdirSync(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...walk(fullPath));
    } else if (/\.(js|mjs|cjs|map|txt|json)$/.test(entry.name)) {
      files.push(fullPath);
    }
  }
  return files;
}

function main() {
  let staticStat;
  try {
    staticStat = statSync(STATIC_DIR);
  } catch {
    console.error(`${STATIC_DIR} does not exist -- run \`npm run build\` first.`);
    process.exitCode = 1;
    return;
  }
  if (!staticStat.isDirectory()) {
    console.error(`${STATIC_DIR} is not a directory.`);
    process.exitCode = 1;
    return;
  }

  const files = walk(STATIC_DIR);
  const findings = [];

  for (const file of files) {
    const content = readFileSync(file, "utf-8");
    for (const { name, pattern } of FORBIDDEN_PATTERNS) {
      if (pattern.test(content)) {
        findings.push({ file: path.relative(FRONTEND_ROOT, file), pattern: name });
      }
    }
  }

  if (findings.length > 0) {
    console.error(`Found ${findings.length} forbidden pattern match(es) in built static assets:`);
    for (const finding of findings) {
      console.error(`  - ${finding.file}: matched "${finding.pattern}"`);
    }
    process.exitCode = 1;
    return;
  }

  console.log(`Scanned ${files.length} built static asset file(s) under ${path.relative(FRONTEND_ROOT, STATIC_DIR)} -- no forbidden patterns found.`);
}

main();
