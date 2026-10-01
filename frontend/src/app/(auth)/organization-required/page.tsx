import { OrganizationList } from "@clerk/nextjs";
import { notFound, redirect } from "next/navigation";
import { AccountStateCard } from "@/components/feedback/AccountStateCard";
import { clerkAppearance } from "@/lib/auth/clerk-appearance";
import { isClerkAuthMode } from "@/lib/config/auth-mode";
import { clerkSessionSource } from "@/lib/auth/backend-auth";

export const dynamic = "force-dynamic";

/** Signed in but no active organization: choose one (organizations are provisioned by an administrator). */
export default async function OrganizationRequiredPage() {
  if (!isClerkAuthMode()) notFound();

  const session = await clerkSessionSource().catch(() => null);
  if (session?.userId && session.orgId) redirect("/dashboard");

  return (
    <>
      <AccountStateCard reason="no-organization" />
      <OrganizationList
        hidePersonal
        afterSelectOrganizationUrl="/dashboard"
        afterCreateOrganizationUrl="/dashboard"
        appearance={clerkAppearance}
      />
    </>
  );
}
