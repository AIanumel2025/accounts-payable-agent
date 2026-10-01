import { notFound } from "next/navigation";
import { AccountStateCard } from "@/components/feedback/AccountStateCard";
import { isAuthReason } from "@/lib/auth/auth-reasons";
import { isClerkAuthMode } from "@/lib/config/auth-mode";

export const dynamic = "force-dynamic";

/**
 * Account-state page: unmapped account, inactive membership, expired session,
 * missing organization, or sign-in verification unavailable. The reason is
 * one of a fixed whitelist -- anything else is shown as "sign in required".
 */
export default async function AccessDeniedPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  if (!isClerkAuthMode()) notFound();

  const { reason } = await searchParams;
  const value = Array.isArray(reason) ? reason[0] : reason;

  return <AccountStateCard reason={isAuthReason(value) ? value : "signed-out"} />;
}
