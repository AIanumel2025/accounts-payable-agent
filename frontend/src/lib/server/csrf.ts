import "server-only";
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

/**
 * CSRF protection for the command POST route (M11C task §7).
 *
 * The app has no user session cookie to ride on (identity is server-side
 * configuration), so a classic cookie-bound synchroniser token is
 * unnecessary. Instead the command route layers four independent checks:
 *
 *   1. `Origin` must be present and its host must equal the request `Host`
 *      (a cross-site form/fetch carries the attacker's origin);
 *   2. `Sec-Fetch-Site`, when the browser sends it, must be `same-origin`;
 *   3. the body must be `application/json` (cross-site JSON POSTs need a
 *      CORS preflight this app never grants);
 *   4. an `X-CSRF-Token` header carrying a short-lived HMAC token bound to
 *      the review-case id. The token is embedded in the server-rendered
 *      page, which a cross-origin attacker cannot read (same-origin
 *      policy), and it is never stored in cookies or browser storage.
 *
 * The HMAC key is `AP_AGENT_FRONTEND_CSRF_SECRET` when set, else a random
 * per-process key cached on `globalThis` (Next bundles pages and route
 * handlers separately, so a plain module variable would differ between
 * them).
 */

export const CSRF_HEADER = "x-csrf-token";
export const CSRF_SECRET_VARIABLE = "AP_AGENT_FRONTEND_CSRF_SECRET";
const DEFAULT_TTL_SECONDS = 8 * 60 * 60;
const GLOBAL_KEY = Symbol.for("ap-agent.frontend.csrf-secret");

type GlobalWithSecret = typeof globalThis & { [GLOBAL_KEY]?: Buffer };

export function csrfSecret(env: Record<string, string | undefined> = process.env): Buffer {
  const configured = env[CSRF_SECRET_VARIABLE];
  if (configured !== undefined && configured.length >= 32) return Buffer.from(configured, "utf-8");
  const holder = globalThis as GlobalWithSecret;
  holder[GLOBAL_KEY] ??= randomBytes(32);
  return holder[GLOBAL_KEY];
}

function sign(secret: Buffer, reviewCaseId: string, expiresAt: number): string {
  return createHmac("sha256", secret).update(`csrf|${reviewCaseId.toLowerCase()}|${expiresAt}`).digest("base64url");
}

export interface CsrfOptions {
  secret?: Buffer;
  nowMs?: number;
  ttlSeconds?: number;
}

export function issueCsrfToken(reviewCaseId: string, options: CsrfOptions = {}): string {
  const now = options.nowMs ?? Date.now();
  const expiresAt = Math.floor(now / 1000) + (options.ttlSeconds ?? DEFAULT_TTL_SECONDS);
  return `${expiresAt}.${sign(options.secret ?? csrfSecret(), reviewCaseId, expiresAt)}`;
}

export function verifyCsrfToken(token: string | null | undefined, reviewCaseId: string, options: CsrfOptions = {}): boolean {
  if (typeof token !== "string" || token.length > 200) return false;
  const [expiryText, signature, ...rest] = token.split(".");
  if (rest.length > 0 || !expiryText || !signature || !/^\d{1,12}$/.test(expiryText)) return false;
  const expiresAt = Number(expiryText);
  const now = Math.floor((options.nowMs ?? Date.now()) / 1000);
  if (expiresAt < now) return false;
  const expected = Buffer.from(sign(options.secret ?? csrfSecret(), reviewCaseId, expiresAt));
  const provided = Buffer.from(signature);
  return expected.length === provided.length && timingSafeEqual(expected, provided);
}

/**
 * Same-origin gate. Fails closed: a missing `Origin` is rejected (browsers
 * always send it on cross-origin and same-origin `fetch` POSTs).
 */
export function isSameOriginRequest(headers: Headers): boolean {
  const fetchSite = headers.get("sec-fetch-site");
  if (fetchSite !== null && fetchSite !== "same-origin") return false;

  const origin = headers.get("origin");
  const host = headers.get("host");
  if (!origin || !host) return false;

  try {
    const parsed = new URL(origin);
    return (parsed.protocol === "http:" || parsed.protocol === "https:") && parsed.host === host;
  } catch {
    return false;
  }
}
