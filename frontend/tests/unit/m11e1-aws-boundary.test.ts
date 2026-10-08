import { createHash, createHmac } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { hasReservedInboundHeader, stripReservedInboundHeaders, UPSTREAM_CLERK_AUTHORIZATION_HEADER } from "@/lib/auth/reserved-headers";
import { loadServerEnvConfig } from "@/lib/config/server-env";
import { loadUploadMode } from "@/lib/config/operations-mode";
import { loadPlatformAuthMode, upstreamFetch, upstreamHeaders } from "@/lib/server/upstream";
import { signRequest } from "../../lambda/aws-sigv4.mjs";
import { loadSsmParameters, parseParameterMapping } from "../../lambda/bootstrap.mjs";

const CREDS = { AWS_REGION: "eu-west-2", AWS_ACCESS_KEY_ID: "AKIDEXAMPLE", AWS_SECRET_ACCESS_KEY: "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", AWS_SESSION_TOKEN: "session-token-value" };
const SIGV4 = { ...CREDS, AP_AGENT_FRONTEND_PLATFORM_AUTH_MODE: "aws_sigv4" };

describe("browser cannot forge the server-to-server identity header", () => {
  it("strips the dedicated header and prototype identity headers from every inbound request", () => {
    const inbound = new Headers({
      [UPSTREAM_CLERK_AUTHORIZATION_HEADER]: "Bearer forged",
      "X-Tenant-ID": "22222222-2222-2222-2222-222222222222",
      "X-Actor-Role": "TENANT_ADMIN",
      "X-Amz-Security-Token": "stolen",
      cookie: "__session=abc",
      "user-agent": "browser",
    });
    expect(hasReservedInboundHeader(inbound)).toBe(true);

    const cleaned = stripReservedInboundHeaders(inbound);

    expect(hasReservedInboundHeader(cleaned)).toBe(false);
    expect(cleaned.get(UPSTREAM_CLERK_AUTHORIZATION_HEADER)).toBeNull();
    expect(cleaned.get("cookie")).toBe("__session=abc"); // ordinary headers survive
    expect(inbound.get(UPSTREAM_CLERK_AUTHORIZATION_HEADER)).toBe("Bearer forged"); // input untouched
  });

  it("discards a pre-existing copy and rebuilds the header only from the Clerk-derived Authorization", () => {
    const headers = upstreamHeaders({ [UPSTREAM_CLERK_AUTHORIZATION_HEADER]: "Bearer forged", Authorization: "Bearer real-session" }, "aws_sigv4");
    expect(headers.get(UPSTREAM_CLERK_AUTHORIZATION_HEADER)).toBe("Bearer real-session");
    expect(headers.get("authorization")).toBeNull();

    const forgedOnly = upstreamHeaders({ [UPSTREAM_CLERK_AUTHORIZATION_HEADER]: "Bearer forged" }, "aws_sigv4");
    expect(forgedOnly.get(UPSTREAM_CLERK_AUTHORIZATION_HEADER)).toBeNull(); // nothing authenticated -> nothing sent
  });

  it("keeps the normal Authorization: Bearer behaviour in bearer mode and never emits the dedicated header", () => {
    const headers = upstreamHeaders({ [UPSTREAM_CLERK_AUTHORIZATION_HEADER]: "Bearer forged", Authorization: "Bearer real-session" }, "bearer");
    expect(headers.get("authorization")).toBe("Bearer real-session");
    expect(headers.get(UPSTREAM_CLERK_AUTHORIZATION_HEADER)).toBeNull();
  });
});

describe("upstreamFetch", () => {
  it("passes requests through unchanged in bearer mode", async () => {
    const fetchImpl = vi.fn(async (_url: string | URL, _init?: RequestInit) => new Response("{}"));
    await upstreamFetch("https://api.internal/health", { headers: { Authorization: "Bearer t" } }, {}, fetchImpl as unknown as typeof fetch);
    const init = fetchImpl.mock.calls[0]![1] as RequestInit;
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer t");
  });

  it("never forwards the dedicated header in bearer mode, and leaves every other request untouched", async () => {
    const fetchImpl = vi.fn(async (_url: string | URL, _init?: RequestInit) => new Response("{}"));
    const plain = { headers: { "X-Tenant-ID": "t" }, method: "GET" };
    await upstreamFetch("https://api.internal/x", plain, {}, fetchImpl as unknown as typeof fetch);
    expect(fetchImpl.mock.calls[0]![1]).toBe(plain);

    await upstreamFetch("https://api.internal/x", { headers: { [UPSTREAM_CLERK_AUTHORIZATION_HEADER]: "Bearer forged", Authorization: "Bearer t" } }, {}, fetchImpl as unknown as typeof fetch);
    const sent = new Headers((fetchImpl.mock.calls[1]![1] as RequestInit).headers);
    expect(sent.get(UPSTREAM_CLERK_AUTHORIZATION_HEADER)).toBeNull();
    expect(sent.get("authorization")).toBe("Bearer t");
  });

  it("signs with SigV4 for the lambda service and moves the Clerk token to the dedicated header", async () => {
    const fetchImpl = vi.fn(async (_url: string | URL, _init?: RequestInit) => new Response("{}"));
    await upstreamFetch(
      "https://abc.lambda-url.eu-west-2.on.aws/api/v1/operations/jobs?page=1",
      { method: "POST", headers: { Authorization: "Bearer clerk-token", "Content-Type": "application/json" }, body: '{"a":1}' },
      SIGV4,
      fetchImpl as unknown as typeof fetch,
    );
    const sent = new Headers((fetchImpl.mock.calls[0]![1] as RequestInit).headers);

    expect(sent.get("authorization")).toMatch(/^AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE\/\d{8}\/eu-west-2\/lambda\/aws4_request, SignedHeaders=.*x-ap-agent-clerk-authorization.*, Signature=[0-9a-f]{64}$/);
    expect(sent.get(UPSTREAM_CLERK_AUTHORIZATION_HEADER)).toBe("Bearer clerk-token");
    expect(sent.get("x-amz-security-token")).toBe("session-token-value");
    expect(sent.get("x-amz-content-sha256")).toBe(createHash("sha256").update('{"a":1}').digest("hex"));
    expect(JSON.stringify([...sent.entries()])).not.toContain("wJalrXUtnFEMI"); // the secret key is never sent
  });

  it("refuses to send without credentials or with an unsignable body", async () => {
    const fetchImpl = vi.fn();
    await expect(upstreamFetch("https://x.on.aws/", {}, { AP_AGENT_FRONTEND_PLATFORM_AUTH_MODE: "aws_sigv4" }, fetchImpl as never)).rejects.toThrow(/credentials/);
    await expect(upstreamFetch("https://x.on.aws/", { method: "POST", body: new FormData() }, SIGV4, fetchImpl as never)).rejects.toThrow(/string or binary/);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("platform mode: default bearer, strict values", () => {
    expect(loadPlatformAuthMode({})).toBe("bearer");
    expect(loadPlatformAuthMode({ AP_AGENT_FRONTEND_PLATFORM_AUTH_MODE: "aws_sigv4" })).toBe("aws_sigv4");
    expect(() => loadPlatformAuthMode({ AP_AGENT_FRONTEND_PLATFORM_AUTH_MODE: "x" })).toThrow();
  });
});

describe("SigV4 signer", () => {
  // AWS's published example (service "service", GET /, no body) -- independent of our code path.
  it("matches the AWS reference signing-key derivation and signature layout", () => {
    const now = new Date("2015-08-30T12:36:00Z");
    const headers = signRequest({
      method: "GET",
      url: "https://example.amazonaws.com/?Param2=value2&Param1=value1",
      headers: {},
      region: "us-east-1",
      service: "service",
      credentials: { accessKeyId: "AKIDEXAMPLE", secretAccessKey: "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY" },
      now,
    });
    // Recompute the reference algorithm independently.
    const payloadHash = createHash("sha256").update("").digest("hex");
    const canonical = ["GET", "/", "Param1=value1&Param2=value2", `host:example.amazonaws.com\nx-amz-content-sha256:${payloadHash}\nx-amz-date:20150830T123600Z\n`, "host;x-amz-content-sha256;x-amz-date", payloadHash].join("\n");
    const scope = "20150830/us-east-1/service/aws4_request";
    const toSign = ["AWS4-HMAC-SHA256", "20150830T123600Z", scope, createHash("sha256").update(canonical).digest("hex")].join("\n");
    const h = (k: Buffer | string, d: string) => createHmac("sha256", k).update(d).digest();
    const key = h(h(h(h("AWS4wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20150830"), "us-east-1"), "service"), "aws4_request");
    const expected = createHmac("sha256", key).update(toSign).digest("hex");

    expect(headers.authorization).toBe(`AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/${scope}, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=${expected}`);
  });

  it("signs a path that needs encoding URI-encoded twice (AWS 'get-space' rule for non-S3 services)", () => {
    const now = new Date("2015-08-30T12:36:00Z");
    const creds = { accessKeyId: "AKIDEXAMPLE", secretAccessKey: "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY" };
    const sign = (path: string) => signRequest({ method: "GET", url: `https://example.amazonaws.com${path}`, region: "us-east-1", service: "service", credentials: creds, now }).authorization;
    const payloadHash = createHash("sha256").update("").digest("hex");
    const canonical = ["GET", "/example%2520space/", "", `host:example.amazonaws.com\nx-amz-content-sha256:${payloadHash}\nx-amz-date:20150830T123600Z\n`, "host;x-amz-content-sha256;x-amz-date", payloadHash].join("\n");
    const scope = "20150830/us-east-1/service/aws4_request";
    const toSign = ["AWS4-HMAC-SHA256", "20150830T123600Z", scope, createHash("sha256").update(canonical).digest("hex")].join("\n");
    const h = (k: Buffer | string, d: string) => createHmac("sha256", k).update(d).digest();
    const key = h(h(h(h("AWS4wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20150830"), "us-east-1"), "service"), "aws4_request");
    expect(sign("/example space/")).toContain(`Signature=${createHmac("sha256", key).update(toSign).digest("hex")}`);
  });

  it("signs the body and replaces any caller-supplied Authorization", () => {
    const headers = signRequest({ method: "POST", url: "https://a.on.aws/x", headers: { Authorization: "old" }, body: "payload", region: "eu-west-2", service: "lambda", credentials: { accessKeyId: "A", secretAccessKey: "B" } });
    expect(headers.authorization).not.toBe("old");
    expect(headers["x-amz-content-sha256"]).toBe(createHash("sha256").update("payload").digest("hex"));
  });
});

describe("bootstrap secret loading", () => {
  it("parses the name=path mapping strictly", () => {
    expect(parseParameterMapping("A_B=/ap/a, C_D=/ap/c")).toEqual({ A_B: "/ap/a", C_D: "/ap/c" });
    for (const bad of ["a=/x", "A_B=relative", "A_B", "=/x"]) expect(() => parseParameterMapping(bad)).toThrow();
  });

  it("loads missing variables from SSM with SigV4, never overwriting, never logging values", async () => {
    const env: Record<string, string | undefined> = { ...CREDS, AP_AGENT_SSM_PARAMETERS: "CLERK_SECRET_KEY=/ap/clerk,KEEP_ME=/ap/keep", KEEP_ME: "explicit" };
    const calls: { url: string; init: RequestInit }[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ Parameter: { Value: "sk_test_value\n" } }), { status: 200 });
    }) as unknown as typeof fetch;

    const loaded = await loadSsmParameters(env, fetchImpl);

    expect(loaded).toEqual(["CLERK_SECRET_KEY"]);
    expect(env.CLERK_SECRET_KEY).toBe("sk_test_value");
    expect(env.KEEP_ME).toBe("explicit");
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe("https://ssm.eu-west-2.amazonaws.com/");
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({ Name: "/ap/clerk", WithDecryption: true });
    expect(new Headers(calls[0]!.init.headers).get("authorization")).toContain("/eu-west-2/ssm/aws4_request");
  });

  it("fails closed with a message that names the variable but not the path", async () => {
    const env: Record<string, string | undefined> = { ...CREDS, AP_AGENT_SSM_PARAMETERS: "CLERK_SECRET_KEY=/ap/very-secret-path" };
    const denied = (async () => new Response("denied", { status: 400 })) as unknown as typeof fetch;
    await expect(loadSsmParameters(env, denied)).rejects.toThrow(/CLERK_SECRET_KEY/);
    await expect(loadSsmParameters(env, denied)).rejects.not.toThrow(/very-secret-path/);
    await expect(loadSsmParameters({ AP_AGENT_SSM_PARAMETERS: "A_B=/x" }, denied)).rejects.toThrow(/credentials/);
  });
});

describe("server configuration for the AWS platform mode", () => {
  const clerk = {
    AP_AGENT_API_BASE_URL: "https://abc.lambda-url.eu-west-2.on.aws",
    AP_AGENT_FRONTEND_AUTH_MODE: "clerk_jwt",
    AP_AGENT_ENVIRONMENT: "hosted",
    NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: "pk_test_abcdefghijkl",
    CLERK_SECRET_KEY: "sk_test_abcdefghijkl",
    AP_AGENT_FRONTEND_CSRF_SECRET: "x".repeat(32),
  };

  it("accepts aws_sigv4 only for a hosted clerk deployment", () => {
    expect(loadServerEnvConfig({ ...clerk, AP_AGENT_FRONTEND_PLATFORM_AUTH_MODE: "aws_sigv4" }).platformAuthMode).toBe("aws_sigv4");
    expect(loadServerEnvConfig(clerk).platformAuthMode).toBeUndefined();
    expect(() => loadServerEnvConfig({ ...clerk, AP_AGENT_ENVIRONMENT: "development", AP_AGENT_FRONTEND_PLATFORM_AUTH_MODE: "aws_sigv4" })).toThrow(/hosted/);
    expect(() => loadServerEnvConfig({ ...clerk, AP_AGENT_FRONTEND_PLATFORM_AUTH_MODE: "weird" })).toThrow();
  });

  it("direct upload is opt-in; anything else keeps the multipart flow", () => {
    expect(loadUploadMode({})).toBe("multipart");
    expect(loadUploadMode({ AP_AGENT_FRONTEND_UPLOAD_MODE: "s3_direct" })).toBe("s3_direct");
    expect(loadUploadMode({ AP_AGENT_FRONTEND_UPLOAD_MODE: "nonsense" })).toBe("multipart");
  });
});
