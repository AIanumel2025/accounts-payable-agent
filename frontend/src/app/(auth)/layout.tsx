import type { ReactNode } from "react";
import { ProductIdentity } from "@/components/application-shell/ProductIdentity";
import styles from "./auth-layout.module.css";

/** Minimal chrome for sign-in and account-state pages (no navigation: nothing here is application data). */
export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className={styles.page}>
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <header className={styles.header}>
        <ProductIdentity />
      </header>
      <main id="main-content" tabIndex={-1} className={styles.main}>
        {children}
      </main>
    </div>
  );
}
