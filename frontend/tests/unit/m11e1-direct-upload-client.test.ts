// @vitest-environment node
import { createHash } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { submitInvoiceDirect } from "@/lib/operations/client";

const PDF_BYTES = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 1, 2, 3]);
const INTENT = "5b6f3c1e-8f0a-4c2d-9a11-0c4f0f7a2b11";
const JOB = "77777777-7777-4777-8777-777777777777";
const file = () => new File([PDF_BYTES], "Invoice 1.pdf", { type: "application/pdf" });
const json = (body: unknown, status: number) => new Response(JSON.stringify(body), { status });

afterEach(() => vi.unstubAllGlobals());

function stubFetch(steps: Array<(url: string, init: RequestInit) => Response>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    const step = steps[calls.length - 1];
    if (!step) throw new Error("unexpected extra request");
    return step(url, init);
  });
  return calls;
}

const intent = () =>
  json({ data: { intent_id: INTENT, upload_url: "https://bucket.s3.eu-west-2.amazonaws.com/", upload_fields: { key: `staging/t/${INTENT}`, policy: "p", "Content-Type": "application/pdf" }, maximum_bytes: 10485760 } }, 201);
const accepted = () => json({ data: { idempotent_replay: false, job: { job_id: JOB, status: "QUEUED" } } }, 202);

describe("submitInvoiceDirect", () => {
  it("fingerprints the file, uploads straight to S3 with the policy fields first and the file last, then finalizes", async () => {
    const calls = stubFetch([intent, () => new Response(null, { status: 204 }), accepted]);

    const result = await submitInvoiceDirect(file(), "csrf-token");

    expect(result.ok).toBe(true);
    expect(calls.map((call) => call.url)).toEqual([
      "/api/v1/operations/upload-intents",
      "https://bucket.s3.eu-west-2.amazonaws.com/",
      `/api/v1/operations/upload-intents/${INTENT}/finalize`,
    ]);

    // Step 1: JSON with the declared properties and the CSRF token; no identity anywhere.
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({
      filename: "Invoice 1.pdf", media_type: "application/pdf", byte_size: 8,
      sha256: createHash("sha256").update(PDF_BYTES).digest("hex"),
    });
    expect((calls[0]!.init.headers as Record<string, string>)["x-csrf-token"]).toBe("csrf-token");

    // Step 2: direct to S3, no application credentials, fields before the file.
    const form = calls[1]!.init.body as FormData;
    expect([...form.keys()]).toEqual(["key", "policy", "Content-Type", "file"]);
    expect(calls[1]!.init.headers).toBeUndefined();

    // Step 3: finalize carries only the CSRF token.
    expect(calls[2]!.init.body).toBeUndefined();
  });

  it("stops and reports the API's code when the intent is refused (no S3 call)", async () => {
    const calls = stubFetch([() => json({ errors: ["ACTION_NOT_PERMITTED"] }, 403)]);
    const result = await submitInvoiceDirect(file(), "t");
    expect(result).toEqual({ ok: false, errors: ["ACTION_NOT_PERMITTED"], status: 403 });
    expect(calls).toHaveLength(1);
  });

  it("reports a storage failure without finalizing", async () => {
    const calls = stubFetch([intent, () => new Response("denied", { status: 403 })]);
    const result = await submitInvoiceDirect(file(), "t");
    expect(result).toEqual({ ok: false, errors: ["STORAGE_UPLOAD_FAILED"], status: 403 });
    expect(calls).toHaveLength(2);
  });

  it("reports a network failure to S3 as a storage failure", async () => {
    stubFetch([intent, () => { throw new TypeError("offline"); }]);
    expect(await submitInvoiceDirect(file(), "t")).toEqual({ ok: false, errors: ["STORAGE_UPLOAD_FAILED"], status: null });
  });

  it("surfaces finalize errors (expired intent) with their codes", async () => {
    stubFetch([intent, () => new Response(null, { status: 204 }), () => json({ errors: ["UPLOAD_INTENT_EXPIRED"] }, 410)]);
    expect(await submitInvoiceDirect(file(), "t")).toEqual({ ok: false, errors: ["UPLOAD_INTENT_EXPIRED"], status: 410 });
  });

  it("cannot proceed in a browser without secure hashing", async () => {
    vi.stubGlobal("crypto", {});
    const calls = stubFetch([]);
    expect(await submitInvoiceDirect(file(), "t")).toEqual({ ok: false, errors: ["HASHING_UNSUPPORTED"], status: null });
    expect(calls).toHaveLength(0);
  });
});
