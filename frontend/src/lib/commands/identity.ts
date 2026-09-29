/**
 * Retry-safe command identity (M11C task §8).
 *
 * One intended user operation == one command UUID + one idempotency key.
 * An identical retry (same case, action and payload at the same observed
 * revisions) reuses both, so a network retry or double submit can never
 * create a second decision; any change of content -- a different action,
 * edited field, or refreshed revision -- mints fresh identity. Nothing is
 * persisted: identity lives only in memory for the life of the page (task
 * §8: no sensitive values in browser storage), and a reload simply starts
 * a new operation, which the backend's revision checks make safe.
 */

import { IDEMPOTENCY_KEY_PATTERN, UUID_PATTERN, type BrowserCommandRequest } from "@/lib/commands/contract";

export interface CommandIdentity {
  commandId: string;
  idempotencyKey: string;
}

export function createCommandIdentity(randomUUID: () => string = () => crypto.randomUUID()): CommandIdentity {
  const commandId = randomUUID();
  const identity = { commandId, idempotencyKey: `ap-ui-${commandId}` };
  if (!UUID_PATTERN.test(identity.commandId) || !IDEMPOTENCY_KEY_PATTERN.test(identity.idempotencyKey)) {
    throw new Error("Generated command identity does not satisfy the backend contract.");
  }
  return identity;
}

export type CommandContent = Omit<BrowserCommandRequest, "command_id" | "idempotency_key">;

/** Canonical, key-order-independent serialisation of the operation's content. */
export function commandContentKey(reviewCaseId: string, content: CommandContent): string {
  const corrections = [...content.corrections]
    .map((c) => ({ ...c, evidence_reference_ids: [...c.evidence_reference_ids].sort() }))
    .sort((a, b) => `${a.field_name}:${a.line_number ?? -1}`.localeCompare(`${b.field_name}:${b.line_number ?? -1}`));
  return JSON.stringify({
    reviewCaseId,
    action: content.action,
    disposition: content.disposition,
    rev: content.observed_review_revision,
    wf: content.observed_workflow_revision,
    reasons: [...content.reason_codes].sort(),
    notes: content.notes,
    corrections,
  });
}

/** Holds the identity of the most recent operation so an identical retry reuses it. */
export class OperationIdentityTracker {
  private lastKey: string | null = null;
  private lastIdentity: CommandIdentity | null = null;

  constructor(private readonly generate: () => CommandIdentity = () => createCommandIdentity()) {}

  identityFor(reviewCaseId: string, content: CommandContent): CommandIdentity {
    const key = commandContentKey(reviewCaseId, content);
    if (this.lastKey === key && this.lastIdentity !== null) return this.lastIdentity;
    this.lastKey = key;
    this.lastIdentity = this.generate();
    return this.lastIdentity;
  }
}
