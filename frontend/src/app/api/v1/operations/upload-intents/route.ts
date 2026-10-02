import { NextResponse } from "next/server";
import { defaultUploadDeps } from "@/lib/server/upload-boundary";
import { handleCreateUploadIntent } from "@/lib/server/upload-intent-boundary";

/** `POST /api/v1/operations/upload-intents` -- step 1 of a staged direct upload (AWS mode). Only `POST` is exported. */

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const result = await handleCreateUploadIntent(request, defaultUploadDeps());
  return NextResponse.json(result.body, { status: result.status, headers: { "Cache-Control": "no-store" } });
}
