import { NextResponse } from "next/server";
import { defaultBoundaryDeps, handleCommandRequest } from "@/lib/server/command-boundary";

/**
 * The only POST route in the application (M11C task §7):
 * `POST /api/v1/review-cases/{uuid}/commands`.
 *
 * Only `POST` is exported, so Next.js itself answers 405 for `GET` and every
 * other method (task §7: "GET access to command paths must remain
 * unavailable"). No catch-all, no sub-paths: any other path never matches
 * this file. All behaviour lives in `handleCommandRequest`.
 */

export const dynamic = "force-dynamic";

export async function POST(request: Request, context: { params: Promise<{ reviewCaseId: string }> }) {
  const { reviewCaseId } = await context.params;
  const result = await handleCommandRequest(request, reviewCaseId, defaultBoundaryDeps());
  return NextResponse.json(result.body, { status: result.status, headers: { "Cache-Control": "no-store" } });
}
