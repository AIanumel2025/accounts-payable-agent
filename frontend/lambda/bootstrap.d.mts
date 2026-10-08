export function parseParameterMapping(raw: string | undefined): Record<string, string>;
export function loadSsmParameters(env?: Record<string, string | undefined>, fetchImpl?: typeof fetch): Promise<string[]>;
