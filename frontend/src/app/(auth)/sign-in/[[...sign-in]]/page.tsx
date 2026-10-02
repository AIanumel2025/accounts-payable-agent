import { SignIn } from "@clerk/nextjs";
import { notFound } from "next/navigation";
import { clerkAppearance } from "@/lib/auth/clerk-appearance";
import { isClerkAuthMode } from "@/lib/config/auth-mode";

export const dynamic = "force-dynamic";

export default function SignInPage() {
  if (!isClerkAuthMode()) notFound();

  return (
    <>
      <h1 className="sr-only">Sign in to the Accounts Payable Agent</h1>
      <SignIn appearance={clerkAppearance} />
    </>
  );
}
