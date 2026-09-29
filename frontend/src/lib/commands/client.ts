import {
  parseCommandFailure,
  parseCommandSuccess,
  type BrowserCommandRequest,
  type CommandSuccess,
} from "@/lib/commands/contract";

/**
 * Browser-side call to the one command route (M11C task §7/§8). The browser
 * sends only the allow-listed command fields plus the page's CSRF token; it
 * never sets tenant/actor/role/authentication headers (the server builds
 * those) and never touches browser storage.
 */

export type CommandOutcome =
  | { ok: true; success: CommandSuccess }
  | { ok: false; httpStatus: number | null; errors: string[]; requestId: string | null };

export type SubmitCommand = (
  reviewCaseId: string,
  csrfToken: string,
  request: BrowserCommandRequest,
) => Promise<CommandOutcome>;

export const submitReviewCommand: SubmitCommand = async (reviewCaseId, csrfToken, request) => {
  let response: Response;
  try {
    response = await fetch(`/api/v1/review-cases/${encodeURIComponent(reviewCaseId)}/commands`, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify(request),
    });
  } catch {
    return { ok: false, httpStatus: null, errors: ["NETWORK_ERROR"], requestId: null };
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { ok: false, httpStatus: response.status, errors: ["MALFORMED_RESPONSE"], requestId: null };
  }

  if (response.status === 200) {
    const success = parseCommandSuccess(body);
    if (success === null) return { ok: false, httpStatus: 200, errors: ["MALFORMED_RESPONSE"], requestId: null };
    return { ok: true, success };
  }

  const failure = parseCommandFailure(body);
  return {
    ok: false,
    httpStatus: response.status,
    errors: failure.errors.length > 0 ? failure.errors : ["INTERNAL_ERROR"],
    requestId: failure.requestId,
  };
};
