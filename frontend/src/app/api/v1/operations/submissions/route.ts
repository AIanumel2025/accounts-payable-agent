import { NextResponse } from "next/server";
import { defaultUploadDeps, handleUploadRequest } from "@/lib/server/upload-boundary";

/**
 * `POST /api/v1/operations/submissions` -- the only file-accepting route in
 * the application (M11D Core). Only `POST` is exported, so Next.js answers
 * 405 for every other method. All behaviour lives in `handleUploadRequest`.
 */

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const result = await handleUploadRequest(request, defaultUploadDeps());
  return NextResponse.json(result.body, { status: result.status, headers: { "Cache-Control": "no-store" } });
}
