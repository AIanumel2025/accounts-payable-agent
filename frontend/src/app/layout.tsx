import { ClerkProvider } from "@clerk/nextjs";
import type { Metadata } from "next";
import type { ReactNode } from "react";
import { clerkAppearance } from "@/lib/auth/clerk-appearance";
import { isClerkAuthMode } from "@/lib/config/auth-mode";
import "@/styles/tokens.css";

export const metadata: Metadata = {
  title: "Accounts Payable Agent — Review",
  description: "Human-review interface for the Accounts Payable Agent.",
};

// Authentication state is per request; nothing in the shell may be statically cached.
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: { children: ReactNode }) {
  const content = (
    <html lang="en-GB">
      <body>{children}</body>
    </html>
  );

  if (!isClerkAuthMode()) return content;

  return (
    <ClerkProvider
      publishableKey={process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY}
      signInUrl="/sign-in"
      signUpUrl="/sign-up"
      signInFallbackRedirectUrl="/dashboard"
      signUpFallbackRedirectUrl="/dashboard"
      afterSignOutUrl="/sign-in"
      appearance={clerkAppearance}
    >
      {content}
    </ClerkProvider>
  );
}
