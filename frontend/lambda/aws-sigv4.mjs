// Minimal AWS Signature Version 4 signer (M11E.1), no dependencies.
//
// Used in exactly two places, both server-side on AWS Lambda:
//   - `src/lib/server/upstream.ts` signs every request from the Next.js server to the
//     IAM-protected FastAPI Function URL (service `lambda`);
//   - `lambda/bootstrap.mjs` signs the SSM `GetParameter` calls that fetch secrets at cold start
//     (service `ssm`).
// Credentials are the Lambda execution role's temporary credentials (environment variables set by
// the Lambda runtime). Nothing here is ever imported by client code.

import { createHash, createHmac } from "node:crypto";

const sha256Hex = (data) => createHash("sha256").update(data).digest("hex");
const hmac = (key, data) => createHmac("sha256", key).update(data).digest();

/** RFC 3986 encoding of one URI path segment / query component. */
function encode(value) {
  return encodeURIComponent(value).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);
}

function canonicalPath(pathname) {
  // Every service except S3 signs the path URI-encoded twice: `pathname` is already encoded once (the form on
  // the wire), so each segment is encoded once more.
  return pathname.split("/").map((segment) => encode(segment)).join("/") || "/";
}

function canonicalQuery(searchParams) {
  return [...searchParams.entries()]
    .map(([key, value]) => [encode(key), encode(value)])
    .sort(([ak, av], [bk, bv]) => (ak < bk ? -1 : ak > bk ? 1 : av < bv ? -1 : av > bv ? 1 : 0))
    .map(([key, value]) => `${key}=${value}`)
    .join("&");
}

export function credentialsFromEnvironment(env = process.env) {
  const accessKeyId = env.AWS_ACCESS_KEY_ID;
  const secretAccessKey = env.AWS_SECRET_ACCESS_KEY;
  if (!accessKeyId || !secretAccessKey) return null;
  return { accessKeyId, secretAccessKey, sessionToken: env.AWS_SESSION_TOKEN || undefined };
}

/**
 * Returns the complete header set to send (lower-case names): the caller's headers plus `host`,
 * `x-amz-date`, `x-amz-content-sha256`, `x-amz-security-token` (when the credentials are
 * temporary) and `authorization`. Any pre-existing `authorization` header is replaced.
 */
export function signRequest({ method, url, headers = {}, body = "", region, service, credentials, now = new Date() }) {
  const target = new URL(url);
  const amzDate = now.toISOString().replace(/[:-]|\.\d{3}/g, "");
  const dateStamp = amzDate.slice(0, 8);
  const payload = typeof body === "string" ? Buffer.from(body, "utf-8") : Buffer.from(body);
  const payloadHash = sha256Hex(payload);

  const merged = {};
  for (const [name, value] of Object.entries(headers)) merged[name.toLowerCase()] = String(value).trim().replace(/\s+/g, " ");
  delete merged.authorization;
  merged.host = target.host;
  merged["x-amz-date"] = amzDate;
  merged["x-amz-content-sha256"] = payloadHash;
  if (credentials.sessionToken) merged["x-amz-security-token"] = credentials.sessionToken;

  const names = Object.keys(merged).sort();
  const canonicalHeaders = names.map((name) => `${name}:${merged[name]}\n`).join("");
  const signedHeaders = names.join(";");
  const canonicalRequest = [
    method.toUpperCase(),
    canonicalPath(target.pathname),
    canonicalQuery(target.searchParams),
    canonicalHeaders,
    signedHeaders,
    payloadHash,
  ].join("\n");

  const scope = `${dateStamp}/${region}/${service}/aws4_request`;
  const stringToSign = ["AWS4-HMAC-SHA256", amzDate, scope, sha256Hex(canonicalRequest)].join("\n");
  const signingKey = hmac(hmac(hmac(hmac(`AWS4${credentials.secretAccessKey}`, dateStamp), region), service), "aws4_request");
  const signature = createHmac("sha256", signingKey).update(stringToSign).digest("hex");

  merged.authorization = `AWS4-HMAC-SHA256 Credential=${credentials.accessKeyId}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`;
  return merged;
}
