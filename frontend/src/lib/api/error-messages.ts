import type { AppErrorKind } from "@/lib/api/dashboard";

/**
 * Maps an internal error kind to user-facing copy (M11A task §10). Every
 * message here is safe to render: no stack trace, DSN, SQL, hostname, or
 * filesystem path ever reaches this layer -- `src/lib/server/backend-request.ts`
 * and `src/lib/config/server-env.ts` already strip those out.
 */

export interface ErrorPresentation {
  title: string;
  description: string;
  retryable: boolean;
}

const ERROR_PRESENTATIONS: Record<AppErrorKind, ErrorPresentation> = {
  CONFIG_ERROR: {
    title: "Authentication is misconfigured",
    description:
      "The server-side connection to the review API is not configured correctly. This is an application configuration issue, not something you can fix by retrying.",
    retryable: false,
  },
  UNAUTHORIZED: {
    title: "Not authenticated",
    description: "The review API rejected this request's credentials. Please contact an administrator.",
    retryable: false,
  },
  FORBIDDEN: {
    title: "Access denied",
    description: "You do not have permission to view this data for this tenant.",
    retryable: false,
  },
  NOT_FOUND: {
    title: "Not found",
    description: "The requested data could not be found.",
    retryable: false,
  },
  TIMEOUT: {
    title: "Request timed out",
    description: "The backend took too long to respond.",
    retryable: true,
  },
  UNAVAILABLE: {
    title: "Backend unavailable",
    description: "The review API could not be reached. It may be starting up or temporarily down.",
    retryable: true,
  },
  MALFORMED_RESPONSE: {
    title: "Unexpected response",
    description: "The backend returned a response this application could not understand.",
    retryable: true,
  },
  UNKNOWN: {
    title: "Something went wrong",
    description: "An unexpected error occurred while loading this data.",
    retryable: true,
  },
};

export function presentError(kind: AppErrorKind): ErrorPresentation {
  return ERROR_PRESENTATIONS[kind];
}
