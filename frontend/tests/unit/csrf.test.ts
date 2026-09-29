// @vitest-environment node
import { randomBytes } from "node:crypto";
import { describe, expect, it } from "vitest";
import { isSameOriginRequest, issueCsrfToken, verifyCsrfToken } from "@/lib/server/csrf";
import { CASE_ID } from "../support/command-fixtures";

const secret = randomBytes(32);
const OTHER_CASE = "99999999-2222-4333-8444-555555555555";

describe("CSRF token (task §7)", () => {
  it("verifies a token issued for the same case", () => {
    expect(verifyCsrfToken(issueCsrfToken(CASE_ID, { secret }), CASE_ID, { secret })).toBe(true);
  });

  it("is bound to the review case (case-insensitively)", () => {
    const token = issueCsrfToken(CASE_ID, { secret });
    expect(verifyCsrfToken(token, OTHER_CASE, { secret })).toBe(false);
    expect(verifyCsrfToken(token, CASE_ID.toUpperCase(), { secret })).toBe(true);
  });

  it("expires", () => {
    const now = 1_700_000_000_000;
    const token = issueCsrfToken(CASE_ID, { secret, nowMs: now, ttlSeconds: 60 });
    expect(verifyCsrfToken(token, CASE_ID, { secret, nowMs: now + 30_000 })).toBe(true);
    expect(verifyCsrfToken(token, CASE_ID, { secret, nowMs: now + 61_000 })).toBe(false);
  });

  it("rejects a token signed with another key", () => {
    const token = issueCsrfToken(CASE_ID, { secret: randomBytes(32) });
    expect(verifyCsrfToken(token, CASE_ID, { secret })).toBe(false);
  });

  it.each([null, undefined, "", "abc", "1.2.3", "9999999999999.sig", ".", "x".repeat(500), "123.", ".sig"])("rejects malformed token %s", (token) => {
    expect(verifyCsrfToken(token, CASE_ID, { secret })).toBe(false);
  });

  it("rejects a tampered expiry", () => {
    const token = issueCsrfToken(CASE_ID, { secret });
    const [expiry, sig] = token.split(".");
    expect(verifyCsrfToken(`${Number(expiry) + 1000}.${sig}`, CASE_ID, { secret })).toBe(false);
  });
});

describe("same-origin gate", () => {
  const headers = (init: Record<string, string>) => new Headers(init);

  it("accepts a same-origin request", () => {
    expect(isSameOriginRequest(headers({ origin: "http://127.0.0.1:3000", host: "127.0.0.1:3000", "sec-fetch-site": "same-origin" }))).toBe(true);
    expect(isSameOriginRequest(headers({ origin: "http://127.0.0.1:3000", host: "127.0.0.1:3000" }))).toBe(true);
  });

  it.each([
    ["cross-site fetch metadata", { origin: "http://127.0.0.1:3000", host: "127.0.0.1:3000", "sec-fetch-site": "cross-site" }],
    ["same-site (sibling) fetch metadata", { origin: "http://127.0.0.1:3000", host: "127.0.0.1:3000", "sec-fetch-site": "same-site" }],
    ["different origin", { origin: "https://evil.example", host: "127.0.0.1:3000" }],
    ["different port", { origin: "http://127.0.0.1:4000", host: "127.0.0.1:3000" }],
    ["missing origin", { host: "127.0.0.1:3000" }],
    ["missing host", { origin: "http://127.0.0.1:3000" }],
    ["opaque origin", { origin: "null", host: "127.0.0.1:3000" }],
    ["non-http origin", { origin: "file://127.0.0.1:3000", host: "127.0.0.1:3000" }],
  ])("rejects %s", (_label, init) => {
    expect(isSameOriginRequest(headers(init))).toBe(false);
  });
});
