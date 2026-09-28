"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { NavItem } from "@/components/application-shell/navigation";
import styles from "./NavList.module.css";

export function NavList({ items, onNavigate }: { items: NavItem[]; onNavigate?: () => void }) {
  const pathname = usePathname();

  return (
    <ul className={styles.list}>
      {items.map((item) => {
        if (item.status === "forthcoming" || !item.href) {
          return (
            <li key={item.label}>
              <span className={styles.disabledItem} aria-disabled="true">
                <span>{item.label}</span>
                <span className={styles.forthcomingTag}>Coming in M11B</span>
              </span>
            </li>
          );
        }

        const isActive = pathname === item.href || pathname?.startsWith(`${item.href}/`);

        return (
          <li key={item.label}>
            <Link
              href={item.href}
              className={styles.navItem}
              data-active={isActive || undefined}
              aria-current={isActive ? "page" : undefined}
              onClick={onNavigate}
            >
              {item.label}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
