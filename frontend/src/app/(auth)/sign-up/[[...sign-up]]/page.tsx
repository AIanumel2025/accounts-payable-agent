import { SignUp } from "@clerk/nextjs";
import { notFound } from "next/navigation";
import { clerkAppearance } from "@/lib/auth/clerk-appearance";
import { isClerkAuthMode } from "@/lib/config/auth-mode";

export const dynamic = "force-dynamic";

/**
 * Reached only through an organization invitation (configure the Clerk
 * instance for restricted/invitation-only sign-up). Creating a Clerk account
 * grants nothing: access still requires an administrator-registered mapping.
 */
export default function SignUpPage() {
  if (!isClerkAuthMode()) notFound();

  return (
    <>
      <h1 className="sr-only">Create your account</h1>
      <SignUp appearance={clerkAppearance} />
    </>
  );
}
