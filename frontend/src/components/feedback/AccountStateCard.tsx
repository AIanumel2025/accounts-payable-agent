"use client";

import { OrganizationSwitcher, SignOutButton } from "@clerk/nextjs";
import { AUTH_REASON_PRESENTATION, type AuthReason } from "@/lib/auth/auth-reasons";
import { clerkAppearance } from "@/lib/auth/clerk-appearance";
import styles from "./States.module.css";

/**
 * A signed-in or signing-in user cannot proceed (M11E): expired session,
 * unmapped account, inactive membership, no active organization, or sign-in
 * verification temporarily unavailable. Copy is fixed per reason; it never
 * contains an identifier, a token or a backend message.
 */
export function AccountStateCard({ reason }: { reason: AuthReason }) {
  const presentation = AUTH_REASON_PRESENTATION[reason];

  return (
    <section className={styles.stateCard} role="alert" aria-labelledby="account-state-title" data-testid="account-state" data-reason={reason}>
      <p className={styles.stateGlyph} aria-hidden="true">
        ◇
      </p>
      <h1 id="account-state-title" className={styles.stateTitle}>
        {presentation.title}
      </h1>
      <p className={styles.stateDescription}>{presentation.message}</p>
      <div className={styles.accountActions}>
        {presentation.actions.includes("switch-organization") && reason !== "no-organization" ? (
          <OrganizationSwitcher hidePersonal afterSelectOrganizationUrl="/dashboard" appearance={clerkAppearance} />
        ) : null}
        {presentation.actions.includes("retry") ? (
          <a className={styles.retryButton} href="/dashboard" data-testid="account-retry">
            Try again
          </a>
        ) : null}
        {presentation.actions.includes("sign-out") ? (
          <SignOutButton redirectUrl="/sign-in">
            <button type="button" className={styles.retryButton} data-testid="account-sign-out">
              {reason === "signed-out" || reason === "session-expired" ? "Sign in again" : "Sign out"}
            </button>
          </SignOutButton>
        ) : null}
      </div>
    </section>
  );
}
