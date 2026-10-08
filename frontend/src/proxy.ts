import { clerkMiddleware } from "@clerk/nextjs/server";
import { NextResponse, type NextFetchEvent, type NextRequest } from "next/server";
import { decideAccess } from "@/lib/auth/route-access";
import { stripReservedInboundHeaders } from "@/lib/auth/reserved-headers";
import { isClerkAuthMode } from "@/lib/config/auth-mode";

/**
 * Hosted authentication boundary (M11E): runs on the server for every
 * request that is not a static asset. In `clerk_jwt` mode nothing
 * application-specific is reachable without a signed-in Clerk user and an
 * active organization (policy: `lib/auth/route-access`). In development mode
 * the proxy is a pass-through and the M11A-M11D header adapter applies.
 *
 * Clerk only establishes *who* is calling and which organization is active.
 * FastAPI independently verifies the session token and maps it to a tenant and
 * role in its own database.
 */

/**
 * Continues to the application with the reserved server-to-server headers removed from the request
 * (M11E.1): a browser can never smuggle `x-ap-agent-clerk-authorization` -- or the prototype identity
 * headers -- into any route handler or page. The Next.js server builds its own upstream identity.
 */
function passThrough(request: NextRequest) {
  return NextResponse.next({ request: { headers: stripReservedInboundHeaders(request.headers) } });
}

const clerkGate = clerkMiddleware(async (auth, request) => {
  const { userId, orgId } = await auth();
  const decision = decideAccess({
    pathname: request.nextUrl.pathname,
    returnTo: `${request.nextUrl.pathname}${request.nextUrl.search}`,
    isSignedIn: Boolean(userId),
    hasOrganization: Boolean(orgId),
  });

  if (decision.action === "redirect") {
    return NextResponse.redirect(new URL(decision.to, request.url));
  }

  if (decision.action === "json") {
    return NextResponse.json(
      { errors: [decision.code], generated_at: new Date().toISOString() },
      { status: decision.status, headers: { "Cache-Control": "no-store" } },
    );
  }

  return passThrough(request);
}, () => {
  // Optional networkless verification key (Clerk "JWT public key", PEM) and the front-end origins
  // permitted to mint session tokens (`azp`) -- both verified again by FastAPI.
  const authorizedParties = (process.env.AP_AGENT_CLERK_AUTHORIZED_PARTIES ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter((value) => value.length > 0);
  return {
    jwtKey: process.env.CLERK_JWT_KEY?.trim() || undefined,
    authorizedParties: authorizedParties.length > 0 ? authorizedParties : undefined,
  };
});

export default function proxy(request: NextRequest, event: NextFetchEvent) {
  if (!isClerkAuthMode()) return passThrough(request);
  return clerkGate(request, event);
}

export const config = {
  // Everything except Next.js internals and static files.
  matcher: ["/((?!_next|.*\\.(?:ico|svg|png|jpg|jpeg|gif|webp|css|js|map|txt|woff2?)$).*)", "/(api)(.*)"],
};
