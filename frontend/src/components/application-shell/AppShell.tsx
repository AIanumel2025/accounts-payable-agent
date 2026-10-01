import type { ReactNode } from "react";
import { MobileNav } from "@/components/application-shell/MobileNav";
import { Sidebar } from "@/components/application-shell/Sidebar";
import styles from "./AppShell.module.css";

/**
 * Root application chrome (M11A task §8): skip-to-content link, desktop
 * sidebar, compact mobile navigation, and the main-content landmark. Page
 * headers (title + status indicators) are rendered by each page/segment
 * via `PageHeader`, inside `{children}`, so they can carry page-specific
 * data (e.g. backend health fetched for that page).
 */
export function AppShell({ children, hosted = false }: { children: ReactNode; hosted?: boolean }) {
  return (
    <div className={styles.shell}>
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <div className={styles.body}>
        <Sidebar hosted={hosted} />
        <div className={styles.mainColumn}>
          <MobileNav hosted={hosted} />
          <main id="main-content" tabIndex={-1} className={styles.main}>
            {children}
          </main>
        </div>
      </div>
    </div>
  );
}
