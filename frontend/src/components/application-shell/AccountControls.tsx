"use client";

import { OrganizationSwitcher, SignOutButton, UserButton } from "@clerk/nextjs";
import { clerkAppearance } from "@/lib/auth/clerk-appearance";
import styles from "./AccountControls.module.css";

/**
 * Signed-in identity in the application shell (hosted mode only): the
 * person's name/avatar and active organization come straight from Clerk
 * (display only -- authorization comes from the database mapping), with an
 * organization switcher and an explicit sign-out. Rendered only below a
 * `ClerkProvider`, i.e. only when the deployment uses Clerk.
 */
export function AccountControls() {
  return (
    <div className={styles.controls} data-testid="account-controls">
      <OrganizationSwitcher
        hidePersonal
        afterSelectOrganizationUrl="/dashboard"
        afterLeaveOrganizationUrl="/organization-required"
        appearance={clerkAppearance}
      />
      <UserButton showName appearance={clerkAppearance} />
      <SignOutButton redirectUrl="/sign-in">
        <button type="button" className={styles.signOut} data-testid="sign-out">
          Sign out
        </button>
      </SignOutButton>
    </div>
  );
}
