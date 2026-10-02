// Test-only stand-in for Clerk used by the hosted acceptance run (M11E): an RSA
// key pair, a local JWKS endpoint FastAPI verifies against, and a minter for
// session tokens. No network access to Clerk, no real instance, no real
// identifier. The private key lives only in this process's memory.

import { createServer } from "node:http";
import { createPrivateKey, createSign, generateKeyPairSync, createPublicKey } from "node:crypto";

const b64url = (input) => Buffer.from(input).toString("base64url");

export function createClerkTestKit({ issuer, authorizedParty, keyId = "test-key-1" }) {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const publicPem = publicKey.export({ type: "spki", format: "pem" }).toString();
  const jwk = { ...publicKey.export({ format: "jwk" }), kid: keyId, use: "sig", alg: "RS256" };
  const signer = createPrivateKey(privateKey.export({ type: "pkcs8", format: "pem" }));

  function mint({ user, org, azp = authorizedParty, expiresInSeconds = 3600, issuedAtOffset = -5, tamper = false } = {}) {
    const now = Math.floor(Date.now() / 1000) + issuedAtOffset;
    const claims = { sub: user, sid: "sess_test", iss: issuer, iat: now, nbf: now, exp: now + expiresInSeconds, v: 2 };
    if (azp) claims.azp = azp;
    if (org) claims.o = { id: org, rol: "admin", slg: "test-org" };
    const signingInput = `${b64url(JSON.stringify({ alg: "RS256", typ: "JWT", kid: keyId }))}.${b64url(JSON.stringify(claims))}`;
    const signature = createSign("RSA-SHA256").update(signingInput).sign(signer).toString("base64url");
    const token = `${signingInput}.${signature}`;
    if (!tamper) return token;
    const [header, , sig] = token.split(".");
    return `${header}.${b64url(JSON.stringify({ ...claims, sub: "user_attacker" }))}.${sig}`;
  }

  function startJwksServer(port) {
    const server = createServer((request, response) => {
      if (request.url === "/.well-known/jwks.json") {
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify({ keys: [jwk] }));
        return;
      }
      response.writeHead(404).end();
    });
    return new Promise((resolve) => server.listen(port, "127.0.0.1", () => resolve(server)));
  }

  /** Cookies a browser would hold for a signed-in production Clerk instance (`__session` + `__client_uat`). */
  function sessionCookies(token, url) {
    const issuedAt = Math.floor(Date.now() / 1000) - 10;
    return [
      { name: "__session", value: token, url },
      { name: "__client_uat", value: String(issuedAt), url },
    ];
  }

  return { publicPem, mint, startJwksServer, sessionCookies };
}

/** A well-formed (but fake) Clerk production key pair for the frontend API host `clerk.accounts.example.test`. */
export function fakeClerkKeys() {
  const frontendApi = "clerk.accounts.example.test";
  return {
    frontendApi,
    publishableKey: `pk_live_${Buffer.from(`${frontendApi}$`).toString("base64")}`,
    secretKey: `sk_live_${"x".repeat(24)}`,
  };
}
