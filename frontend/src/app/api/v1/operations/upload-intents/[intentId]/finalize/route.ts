import { NextResponse } from "next/server";
import { defaultUploadDeps } from "@/lib/server/upload-boundary";
import { handleFinalizeUploadIntent } from "@/lib/server/upload-intent-boundary";

/** `POST /api/v1/operations/upload-intents/{id}/finalize` -- step 3 of a staged direct upload (AWS mode). */

export const dynamic = "force-dynamic";

export async function POST(request: Request, context: { params: Promise<{ intentId: string }> }) {
  const { intentId } = await context.params;
  const result = await handleFinalizeUploadIntent(request, intentId, defaultUploadDeps());
  return NextResponse.json(result.body, { status: result.status, headers: { "Cache-Control": "no-store" } });
}
