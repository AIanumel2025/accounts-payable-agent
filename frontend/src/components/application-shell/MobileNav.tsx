"use client";

import { useState } from "react";
import { NAV_ITEMS } from "@/components/application-shell/navigation";
import { NavList } from "@/components/application-shell/NavList";
import { ProductIdentity } from "@/components/application-shell/ProductIdentity";
import styles from "./MobileNav.module.css";

/** Compact mobile navigation (M11A task §8/§13): a toggled drawer below the tablet breakpoint. */
export function MobileNav() {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <div className={styles.wrapper}>
      <div className={styles.bar}>
        <ProductIdentity />
        <button
          type="button"
          className={styles.toggle}
          aria-expanded={isOpen}
          aria-controls="mobile-nav-drawer"
          onClick={() => setIsOpen((open) => !open)}
        >
          <span className="sr-only">{isOpen ? "Close navigation" : "Open navigation"}</span>
          <span aria-hidden="true">{isOpen ? "✕" : "☰"}</span>
        </button>
      </div>
      {isOpen ? (
        <nav id="mobile-nav-drawer" className={styles.drawer} aria-label="Primary">
          <NavList items={NAV_ITEMS} onNavigate={() => setIsOpen(false)} />
        </nav>
      ) : null}
    </div>
  );
}
