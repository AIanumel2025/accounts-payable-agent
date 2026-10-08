// Lambda entry point for the Next.js server (M11E.1).
//
// 1. Reads the *names* of the secrets from AP_AGENT_SSM_PARAMETERS
//    ("ENV_NAME=/ssm/path,OTHER=/ssm/other") and fetches each SecureString from SSM Parameter Store with
//    the function's execution role (SigV4-signed, decrypted by SSM). Values go into process.env only --
//    never to disk, to a log or to a build artifact. An already-set variable is not overwritten.
// `entrypoint.mjs` calls this and then starts the Next.js standalone server. It fails closed: if a configured
// secret cannot be loaded the process exits non-zero before the server listens, so the function never serves
// requests with missing credentials.

import { credentialsFromEnvironment, signRequest } from "./aws-sigv4.mjs";

const NAME = /^[A-Z][A-Z0-9_]{1,80}$/;
const PATH = /^\/[A-Za-z0-9_.\-/]{1,1000}$/;

export function parseParameterMapping(raw) {
  const mapping = {};
  for (const item of String(raw ?? "").split(",").map((part) => part.trim()).filter(Boolean)) {
    const separator = item.indexOf("=");
    const name = separator < 0 ? "" : item.slice(0, separator);
    const path = separator < 0 ? "" : item.slice(separator + 1);
    if (!NAME.test(name) || !PATH.test(path)) throw new Error("AP_AGENT_SSM_PARAMETERS is malformed.");
    mapping[name] = path;
  }
  return mapping;
}

export async function loadSsmParameters(env = process.env, fetchImpl = fetch) {
  const mapping = parseParameterMapping(env.AP_AGENT_SSM_PARAMETERS);
  const pending = Object.entries(mapping).filter(([name]) => !(env[name] ?? "").trim());
  if (pending.length === 0) return [];

  const region = env.AWS_REGION;
  const credentials = credentialsFromEnvironment(env);
  if (!region || !credentials) throw new Error("AWS credentials or region are not available to load secrets.");

  const loaded = [];
  for (const [name, path] of pending) {
    const body = JSON.stringify({ Name: path, WithDecryption: true });
    const headers = signRequest({
      method: "POST",
      url: `https://ssm.${region}.amazonaws.com/`,
      headers: { "content-type": "application/x-amz-json-1.1", "x-amz-target": "AmazonSSM.GetParameter" },
      body,
      region,
      service: "ssm",
      credentials,
    });
    const response = await fetchImpl(`https://ssm.${region}.amazonaws.com/`, { method: "POST", headers, body });
    if (!response.ok) throw new Error(`${name} could not be loaded from the secret store (HTTP ${response.status}).`);
    const value = (await response.json())?.Parameter?.Value;
    if (typeof value !== "string" || value.trim() === "") throw new Error(`${name} is empty in the secret store.`);
    env[name] = value.trim();
    loaded.push(name);
  }
  return loaded;
}
