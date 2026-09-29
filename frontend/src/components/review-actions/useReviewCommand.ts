"use client";

import { useCallback, useRef, useState } from "react";
import { submitReviewCommand, type CommandOutcome, type SubmitCommand } from "@/lib/commands/client";
import type { SupportedAction } from "@/lib/commands/contract";
import { OperationIdentityTracker, type CommandContent } from "@/lib/commands/identity";

export type CommandState =
  | { phase: "idle" }
  | { phase: "pending"; action: SupportedAction }
  | { phase: "done"; action: SupportedAction; outcome: CommandOutcome; seq: number };

interface Options {
  reviewCaseId: string;
  csrfToken: string;
  submit?: SubmitCommand;
  /** Called after a committed (or idempotent) result so the caller can re-read authoritative state. */
  onExecuted: () => void;
}

/**
 * One command at a time (M11C task §8/§9). A synchronous ref guards against a
 * double click racing React's state update; identity is reused for an
 * identical retry and minted fresh for any change of content.
 */
export function useReviewCommand({ reviewCaseId, csrfToken, submit = submitReviewCommand, onExecuted }: Options) {
  const [state, setState] = useState<CommandState>({ phase: "idle" });
  const inFlight = useRef(false);
  const seq = useRef(0);
  const tracker = useRef<OperationIdentityTracker | null>(null);
  if (tracker.current === null) tracker.current = new OperationIdentityTracker();

  const run = useCallback(
    async (content: CommandContent): Promise<CommandOutcome | null> => {
      if (inFlight.current) return null;
      inFlight.current = true;
      setState({ phase: "pending", action: content.action });
      try {
        const identity = tracker.current!.identityFor(reviewCaseId, content);
        const outcome = await submit(reviewCaseId, csrfToken, {
          command_id: identity.commandId,
          idempotency_key: identity.idempotencyKey,
          ...content,
        });
        seq.current += 1;
        setState({ phase: "done", action: content.action, outcome, seq: seq.current });
        if (outcome.ok && outcome.success.data.kind !== "VALIDATED") onExecuted();
        return outcome;
      } finally {
        inFlight.current = false;
      }
    },
    [csrfToken, onExecuted, reviewCaseId, submit],
  );

  const clear = useCallback(() => setState({ phase: "idle" }), []);

  return { state, run, clear };
}
