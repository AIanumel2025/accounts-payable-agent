"use client";

import { useCallback, useTransition } from "react";
import { ReviewActionWorkspace, type ReviewActionWorkspaceProps } from "@/components/review-actions/ReviewActionWorkspace";
import { useRouter } from "next/navigation";

/**
 * Thin client wrapper that wires the workspace to Next.js: after a committed
 * command, `router.refresh()` re-runs the Server Component tree, re-reading
 * the detail, decision history, timeline and capabilities from PostgreSQL
 * (M11C task §9/§20). Dashboard and queue pages are `force-dynamic`, so their
 * next visit re-queries the database too; nothing is optimistic.
 */
export function ReviewActions(props: Omit<ReviewActionWorkspaceProps, "onRefresh">) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const onRefresh = useCallback(() => startTransition(() => router.refresh()), [router]);
  return <ReviewActionWorkspace {...props} onRefresh={onRefresh} />;
}
